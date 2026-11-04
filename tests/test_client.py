"""client.py contracts that the e2e run depends on but no CLI checks for us."""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import client


class WakeBridgeTests(unittest.TestCase):
    def talk(self, *messages):
        env = {**os.environ, "AGENTCHATTR_SERVER": "http://127.0.0.1:9", "AGENTCHATTR_TOKEN": "t"}
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, str(ROOT / "client.py"), "wake-bridge"], input=stdin,
                             capture_output=True, text=True, env=env, timeout=20).stdout
        return [json.loads(line) for line in out.splitlines()]

    def test_declares_the_channel_and_never_negotiates_a_modern_revision(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2099-01-01", "capabilities": {}}}
        [reply] = self.talk(init)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["capabilities"], {"experimental": {"claude/channel": {}}})
        init["params"]["protocolVersion"] = "2025-03-26"
        self.assertEqual(self.talk(init)[0]["result"]["protocolVersion"], "2025-03-26")

    def test_answers_ping_and_rejects_unknown_requests(self):
        replies = self.talk({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/call"})
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32601)


class CodexLaunchTests(unittest.TestCase):
    def test_private_server_watcher_and_queue_share_one_socket(self):
        flags = ["-c", 'mcp_servers.agentchattr.url="http://chat:8200/mcp"']
        reporter = MagicMock()
        server = MagicMock()
        server.poll.return_value = None

        def start(args, **kwargs):
            # Stand in for the external server binding its Unix socket.
            Path(args[args.index("--listen") + 1].removeprefix("unix://")).touch()
            return server

        link = client.CodexLink(["codex"], ROOT, {}, reporter, "test", mcp_flags=flags)
        with (patch.object(client.subprocess, "Popen", side_effect=start) as launch,
              patch.object(client, "CodexRpc") as rpc,
              patch.object(client.subprocess, "run") as queue_run):
            rpc.return_value.loaded_threads.return_value = []
            queue_run.return_value.returncode = 0
            try:
                self.assertTrue(link.connect())
                socket_path = rpc.call_args.kwargs["socket_path"]
                self.assertEqual(link.remote, "unix://" + socket_path)
                self.assertEqual(launch.call_args.args[0],
                                 ["codex", "app-server", *flags, "--listen", link.remote])
                self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)
                link.thread_id = "our-thread"
                link._linked.set()
                link.deliver("wake")
                self.assertEqual(queue_run.call_args.args[0], [
                    "codex", "queue", "--thread", "our-thread", "--message", "wake",
                    "--resume", "--remote", link.remote])
            finally:
                link.close()
            self.assertFalse(Path(socket_path).parent.exists())
            server.terminate.assert_called_once()

    def test_mcp_overrides_are_hosted_on_the_same_server_as_the_tui(self):
        # Without --remote, CLI config overrides select an embedded server,
        # invisible to CodexLink even though chat registration succeeds.
        api = client.Api("http://chat-server:8300", "test-token")
        me = {"name": "codex-test", "mcp_url": "http://chat-server:8200/mcp"}
        with (patch.object(client, "_codex_argv", return_value=["codex"]),
              patch.object(client, "ActivityReporter"),
              patch.object(client, "CodexLink") as link,
              patch.object(client.threading, "Thread"),
              patch.object(client.subprocess, "Popen") as popen):
            link.return_value.connect.return_value = True
            link.return_value.remote = "unix:///tmp/client-only.sock"
            popen.return_value.wait.return_value = 0
            self.assertEqual(client.run_codex(api, me, ROOT, ["--model", "chosen-model"]), 0)
        args = popen.call_args.args[0]
        self.assertIn("--remote", args)
        self.assertEqual(args[args.index("--remote") + 1], "unix:///tmp/client-only.sock")
        flags = link.call_args.kwargs["mcp_flags"]
        overrides = [flags[i + 1] for i, arg in enumerate(flags) if arg == "-c"]
        config = tomllib.loads("\n".join(overrides))
        self.assertEqual(config["mcp_servers"]["agentchattr"], {
            "url": "http://chat-server:8200/mcp",
            "http_headers": {"Authorization": "Bearer test-token"},
            "default_tools_approval_mode": "approve",
        })
        self.assertEqual(args[-2:], ["--model", "chosen-model"])
        self.assertNotIn("test-token", " ".join(args))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_cmd_shim_is_bypassed_through_node_when_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "codex.cmd"
            shim.write_text("")
            script = Path(tmp) / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            script.parent.mkdir(parents=True)
            script.write_text("")
            which = {"codex": str(shim), "node": "/usr/bin/node"}.get
            with patch.object(client.shutil, "which", which):
                self.assertEqual(client._codex_argv(), ["/usr/bin/node", str(script)])
            script.unlink()
            with patch.object(client.shutil, "which", which):
                self.assertEqual(client._codex_argv(), [str(shim)])

    def test_shell_safe_removes_what_cmd_exe_would_interpret(self):
        text = client._shell_safe('say "hi" & calc | x > y %PATH% ^\nnext')
        for ch in '"&|<>%^\n':
            self.assertNotIn(ch, text)
        self.assertIn("say 'hi'", text)

    def test_child_env_bypasses_proxies_for_the_chat_server(self):
        with patch.dict(os.environ, {"NO_PROXY": "corp.local", "CLAUDECODE": "1"}):
            env = client._child_env("http://chat-server:8300")
        self.assertEqual(env["NO_PROXY"], "corp.local,chat-server")
        self.assertNotIn("CLAUDECODE", env)


if __name__ == "__main__":
    unittest.main()
