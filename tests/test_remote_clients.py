"""Per-client tokens, token identities in the registry, remote wake-ups and room members."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import mcp_bridge
from agents import AgentTrigger, RemoteOutbox
from registry import RuntimeRegistry
from router import Router
from store import MessageStore
from tokens import TokenStore, is_local_request

LEGACY_AGENTS = {"claude": {"label": "Claude", "color": "#da7756"},
                 "codex": {"label": "Codex", "color": "#10a37f"}}


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "tokens.json"
        self.tokens = TokenStore(self.path, reserved_bases=LEGACY_AGENTS)

    def test_token_resolves_to_its_client_and_only_a_digest_is_stored(self):
        token = self.tokens.add("claude-pc1", "claude")
        self.assertEqual(self.tokens.resolve(token)["name"], "claude-pc1")
        self.assertEqual(self.tokens.resolve(token)["kind"], "claude")
        self.assertIsNone(self.tokens.resolve(token + "x"))
        self.assertNotIn(token, self.path.read_text("utf-8"))

    def test_names_that_shadow_config_agents_or_reserved_words_are_rejected(self):
        for name in ("claude", "codex-2", "system", "all", "Bad Name", "-x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tokens.add(name, "codex")
        self.tokens.add("claude-pc1", "claude")  # family prefix with a non-numeric suffix is fine
        with self.assertRaises(ValueError):
            self.tokens.add("claude-pc1", "claude")  # duplicate

    def test_changes_from_the_cli_are_picked_up_and_revoke_takes_effect(self):
        server_view = TokenStore(self.path, reserved_bases=LEGACY_AGENTS)
        token = self.tokens.add("maslov", "human")  # another process (the CLI) writes the file
        self.assertEqual(server_view.resolve(token)["name"], "maslov")
        self.assertTrue(self.tokens.revoke("maslov"))
        time.sleep(0.01)
        self.path.touch()
        self.assertIsNone(server_view.resolve(token))

    def test_local_request_needs_loopback_client_and_loopback_host_name(self):
        self.assertTrue(is_local_request("127.0.0.1", "127.0.0.1:8300"))
        self.assertTrue(is_local_request("::1", "[::1]:8200"))
        self.assertTrue(is_local_request("127.0.0.1", "localhost"))
        self.assertFalse(is_local_request("10.0.0.7", "10.0.0.5:8300"))
        # DNS rebinding: loopback connection, foreign Host header
        self.assertFalse(is_local_request("127.0.0.1", "evil.example:8300"))


class FixedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tokens = TokenStore(self.tmp / "tokens.json", reserved_bases=LEGACY_AGENTS)
        self.token = self.tokens.add("codex-pc1", "codex")

    def make_registry(self):
        registry = RuntimeRegistry(data_dir=str(self.tmp))
        registry.seed(LEGACY_AGENTS)
        registry.sync_fixed(self.tokens.agents())
        registry.token_resolver = lambda t: (self.tokens.resolve(t) or {}).get("name")
        return registry

    def test_token_identity_survives_restart_without_being_persisted(self):
        registry = self.make_registry()
        self.assertEqual(registry.resolve_token(self.token)["name"], "codex-pc1")
        self.assertTrue(registry.is_registered("codex-pc1"))
        persisted = (self.tmp / "registry.json").read_text("utf-8") if (self.tmp / "registry.json").exists() else ""
        self.assertNotIn(self.token, persisted)
        restarted = self.make_registry()
        self.assertEqual(restarted.resolve_token(self.token)["name"], "codex-pc1")

    def test_token_identity_cannot_be_renamed_claimed_away_or_registered_locally(self):
        registry = self.make_registry()
        registry.resolve_token(self.token)
        self.assertIsInstance(registry.rename("codex-pc1", "other"), str)
        self.assertIsInstance(registry.claim("codex-pc1", "other"), str)
        self.assertIsNone(registry.register("codex-pc1"))
        self.assertEqual(registry.register("codex")["name"], "codex")  # legacy family untouched

    def test_revoked_or_reissued_token_stops_working_at_once(self):
        registry = self.make_registry()
        registry.resolve_token(self.token)
        self.tokens.revoke("codex-pc1")
        new_token = self.tokens.add("codex-pc1", "codex")
        registry.sync_fixed(self.tokens.agents())
        self.assertIsNone(registry.resolve_token(self.token))
        self.assertEqual(registry.resolve_token(new_token)["name"], "codex-pc1")
        self.tokens.revoke("codex-pc1")
        registry.sync_fixed(self.tokens.agents())
        self.assertFalse(registry.is_registered("codex-pc1"))
        self.assertIsNone(registry.resolve_token(new_token))

    def test_crash_timeout_deregistration_is_recovered_by_the_same_token(self):
        registry = self.make_registry()
        registry.resolve_token(self.token)
        registry.deregister("codex-pc1", reclaimable=True)
        self.assertEqual(registry.resolve_token(self.token)["name"], "codex-pc1")


class OutboxTests(unittest.TestCase):
    def test_acknowledged_entries_drop_and_a_new_boot_resets_the_cursor(self):
        box = RemoteOutbox()
        box.push("a", {"channel": "general"})
        box.push("a", {"channel": "dev"})
        first = box.take("a", 0, box.boot)
        self.assertEqual([e["channel"] for _, e in first], ["general", "dev"])
        self.assertEqual(box.take("a", first[0][0], box.boot)[0][1]["channel"], "dev")
        self.assertEqual(box.take("a", first[1][0], box.boot), [])
        box.push("a", {"channel": "x"})
        self.assertEqual(len(box.take("a", 999, "old-boot")), 1)

    def test_token_clients_get_the_outbox_and_legacy_agents_the_queue_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = TokenStore(Path(tmp) / "tokens.json", reserved_bases=LEGACY_AGENTS)
            tokens.add("claude-pc1", "claude")
            registry = RuntimeRegistry(data_dir=tmp)
            registry.seed(LEGACY_AGENTS)
            registry.sync_fixed(tokens.agents())
            registry.register("codex")
            box = RemoteOutbox()
            trigger = AgentTrigger(registry, data_dir=tmp, remote=box)
            self.assertTrue(trigger.is_available("claude-pc1"))  # queues while offline
            asyncio.run(trigger.trigger("claude-pc1", message="ben: hi", channel="dev"))
            trigger.trigger_sync("codex", message="ben: hi", channel="dev")
            self.assertEqual(box.take("claude-pc1", 0, box.boot)[0][1]["channel"], "dev")
            self.assertFalse((Path(tmp) / "claude-pc1_queue.jsonl").exists())
            self.assertIn('"dev"', (Path(tmp) / "codex_queue.jsonl").read_text("utf-8"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tokens = TokenStore(self.tmp / "tokens.json", reserved_bases=LEGACY_AGENTS)
        self.human = self.tokens.add("maslov", "human")
        self.claude = self.tokens.add("claude-pc1", "claude")
        self.codex = self.tokens.add("codex-pc1", "codex")
        self.registry = RuntimeRegistry(data_dir=str(self.tmp))
        self.registry.seed(LEGACY_AGENTS)
        self.box = RemoteOutbox()
        self.store = MessageStore(str(self.tmp / "log.jsonl"))
        self.router = Router(agent_names=list(LEGACY_AGENTS), default_mention="none", max_hops=4)
        self.registry.on_change(lambda: self.router.update_agents(list(self.registry.get_bases())))
        config = {"server": {"port": 8300}, "mcp": {"http_port": 8200}, "agents": LEGACY_AGENTS}
        settings = {"channels": ["general", "dev"], "username": "user"}
        api = FastAPI()
        for path, fn, methods in [
            ("/api/agent/me", app.agent_me, ["GET"]),
            ("/api/agent/wait", app.agent_wait, ["GET"]),
            ("/api/agent/activity", app.agent_activity, ["POST"]),
            ("/api/agent/disconnect", app.agent_disconnect, ["POST"]),
            ("/api/status", app.get_status, ["GET"]),
        ]:
            api.add_api_route(path, fn, methods=methods)
        patches = {
            "app": api, "config": config, "registry": self.registry, "tokens": self.tokens,
            "remote_outbox": self.box, "store": self.store, "router": self.router,
            "agents": AgentTrigger(self.registry, data_dir=str(self.tmp), remote=self.box),
            "room_settings": settings, "session_engine": None, "ws_clients": set(),
            "session_token": "local-session",
        }
        for name, value in patches.items():
            stack.enter_context(patch.object(app, name, value))
        stack.enter_context(patch.dict(mcp_bridge._presence, clear=True))
        self.registry.token_resolver = app._resolve_fixed_token
        app._sync_tokens(force=True)
        app._install_security_middleware("local-session", config)
        self.client = stack.enter_context(TestClient(api))

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_agent_endpoints_require_an_agent_token(self):
        self.assertEqual(self.client.get("/api/agent/me").status_code, 401)
        self.assertEqual(self.client.get("/api/agent/me", headers=self.bearer(self.human)).status_code, 401)
        me = self.client.get("/api/agent/me", headers=self.bearer(self.codex)).json()
        self.assertEqual((me["name"], me["kind"]), ("codex-pc1", "codex"))
        self.assertEqual(me["mcp_url"], "http://testserver:8200/mcp")

    def test_human_tokens_unlock_the_browser_api_and_unknown_tokens_do_not(self):
        self.assertEqual(self.client.get("/api/status", headers={"X-Session-Token": self.human}).status_code, 200)
        self.assertEqual(self.client.get("/api/status", headers={"X-Session-Token": "nope"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status", headers={"X-Session-Token": self.codex}).status_code, 403)

    def test_same_host_origin_is_allowed_and_foreign_origins_are_not(self):
        ok = self.client.get("/api/status", headers={"X-Session-Token": self.human,
                                                     "Origin": "http://testserver"})
        bad = self.client.get("/api/status", headers={"X-Session-Token": self.human,
                                                      "Origin": "http://evil.example"})
        self.assertEqual((ok.status_code, bad.status_code), (200, 403))

    def test_mention_wakes_the_member_and_long_poll_delivers_it(self):
        asyncio.run(app._handle_new_message({"sender": "maslov", "text": "@codex-pc1 run the tests",
                                             "channel": "dev"}))
        resp = self.client.get("/api/agent/wait", params={"timeout": 0}, headers=self.bearer(self.codex)).json()
        self.assertEqual(len(resp["triggers"]), 1)
        self.assertEqual(resp["triggers"][0]["channel"], "dev")
        self.assertTrue(mcp_bridge.is_online("codex-pc1"))
        again = self.client.get("/api/agent/wait", headers=self.bearer(self.codex),
                                params={"timeout": 0, "after": resp["triggers"][0]["seq"],
                                        "boot": resp["boot"]}).json()
        self.assertEqual(again["triggers"], [])

    def test_room_members_limit_who_is_woken(self):
        app.room_settings["channel_members"] = {"dev": ["claude-pc1"]}
        asyncio.run(app._handle_new_message({"sender": "maslov", "text": "@codex-pc1 @claude-pc1 hi",
                                             "channel": "dev"}))
        self.assertEqual(len(self.box.take("claude-pc1", 0, self.box.boot)), 1)
        self.assertEqual(self.box.take("codex-pc1", 0, self.box.boot), [])
        notices = [m["text"] for m in self.store.get_recent(10, channel="dev") if m["sender"] == "system"]
        self.assertTrue(any("Not a member of #dev: @codex-pc1" in n for n in notices))
        # Other rooms stay open
        asyncio.run(app._handle_new_message({"sender": "maslov", "text": "@codex-pc1 hi",
                                             "channel": "general"}))
        self.assertEqual(len(self.box.take("codex-pc1", 0, self.box.boot)), 1)

    def test_activity_and_disconnect(self):
        h = self.bearer(self.claude)
        self.assertEqual(self.client.post("/api/agent/activity", json={"active": True}, headers=h).status_code, 200)
        self.assertTrue(mcp_bridge.is_active("claude-pc1"))
        mcp_bridge.set_role("claude-pc1", "Reviewer")
        self.addCleanup(mcp_bridge.set_role, "claude-pc1", "")
        self.assertEqual(self.client.post("/api/agent/disconnect", json={}, headers=h).status_code, 200)
        self.assertFalse(self.registry.is_registered("claude-pc1"))
        self.assertEqual(mcp_bridge.get_role("claude-pc1"), "Reviewer")  # name is stable, role stays
        self.assertEqual(self.client.get("/api/agent/me", headers=h).status_code, 200)  # comes back


class McpGateTests(unittest.TestCase):
    def call(self, client_host, host, token=""):
        sent = []

        async def inner(scope, receive, send):
            sent.append("app")

        async def send(msg):
            sent.append(msg.get("status"))

        headers = [(b"host", host.encode())]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "client": (client_host, 1234), "headers": headers}
        asyncio.run(mcp_bridge._TokenGate(inner)(scope, None, send))
        return sent[0]

    def test_remote_mcp_needs_a_registered_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeRegistry(data_dir=tmp)
            registry.seed(LEGACY_AGENTS)
            token = registry.register("claude")["token"]
            with patch.object(mcp_bridge, "registry", registry):
                self.assertEqual(self.call("127.0.0.1", "127.0.0.1:8200"), "app")
                self.assertEqual(self.call("10.0.0.7", "10.0.0.5:8200"), 401)
                self.assertEqual(self.call("127.0.0.1", "evil.example:8200"), 401)
                self.assertEqual(self.call("10.0.0.7", "10.0.0.5:8200", token), "app")


if __name__ == "__main__":
    unittest.main()
