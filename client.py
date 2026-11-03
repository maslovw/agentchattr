"""Join Claude Code or Codex on this machine to an agentchattr server on the LAN.

No tmux and no typing into the terminal:
  - Claude Code: a local channel server (MCP over stdio, started by Claude) gets
    each @mention from the chat server and pushes it into the session as a
    channel event, which wakes the session when it is idle.
  - Codex: the TUI runs on Codex's shared app-server daemon. Each @mention is
    delivered with `codex queue --thread <id> --resume`, which starts a turn.

The token decides who you are and whether Claude or Codex is started. Create it
on the server with `python tokens.py add <name> --kind claude|codex`.

Usage:
    python client.py <profile>                  # [profiles.<profile>] in client.local.toml
    python client.py --server URL --token TOKEN [--cwd DIR] [-- extra CLI args]
"""

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WAKE_SERVER = "agentchattr-wake"
# Channels are only delivered on these MCP revisions (newer ones have no
# unsolicited-notification path), so the bridge never negotiates past them.
_CHANNEL_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# The chat server is on the LAN: never route it through a corporate proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str):
    print(f"  [agentchattr] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Chat server API
# ---------------------------------------------------------------------------

class AuthError(Exception):
    pass


class Api:
    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, timeout: float = 10):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.server + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthError(f"server rejected the token ({exc.code})") from None
            raise

    def me(self, announce: bool = False) -> dict:
        return self.call("GET", "/api/agent/me" + ("?announce=1" if announce else ""))

    def activity(self, active: bool):
        self.call("POST", "/api/agent/activity", {"active": active}, timeout=5)

    def disconnect(self):
        self.call("POST", "/api/agent/disconnect", {}, timeout=5)


def poll_triggers(api: Api, deliver, stop: threading.Event):
    """Long-poll the server for wake-ups and pass each batch to deliver(list)."""
    after, boot, backoff, offline = 0, "", 1.0, False
    while not stop.is_set():
        try:
            query = urllib.parse.urlencode({"after": after, "boot": boot, "timeout": 20})
            resp = api.call("GET", f"/api/agent/wait?{query}", timeout=35)
        except AuthError as exc:
            log(f"{exc}; stopping. Was the token revoked?")
            stop.set()
            return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if not offline:
                log(f"chat server unreachable ({exc}); retrying")
                offline = True
            stop.wait(backoff)
            backoff = min(backoff * 2, 15)
            continue
        if offline:
            log("chat server reachable again")
        offline, backoff = False, 1.0
        boot = resp.get("boot", "")
        triggers = resp.get("triggers", [])
        if not triggers:
            continue
        after = max(t["seq"] for t in triggers)
        try:
            deliver(triggers)
        except Exception as exc:
            log(f"could not deliver wake-up: {exc}")


class PromptBuilder:
    """Turn queued triggers into the wake prompt (same wording as wrapper.py)."""

    def __init__(self, api: Api, name: str):
        self.api = api
        self.name = name
        self._rules_epoch = 0
        self._count = 0

    def build(self, triggers: list[dict]) -> str:
        channel, job_id, custom = "general", None, ""
        for t in triggers:
            channel = t.get("channel", channel)
            job_id = t.get("job_id", job_id)
            if isinstance(t.get("prompt"), str) and t["prompt"].strip():
                custom = t["prompt"].strip()
        if custom:
            prompt = custom
        elif job_id is not None:
            prompt = (f"use mcp to read job_id={job_id} - you're mentioned in a job thread, "
                      "take appropriate action and respond")
        else:
            prompt = f"use mcp to read #{channel} - you're mentioned, take appropriate action and respond"

        try:
            role = (self.api.call("GET", "/api/roles", timeout=5) or {}).get(self.name, "")
        except Exception:
            role = ""
        if role:
            prompt += f"\n\nROLE: {role}"

        # Rules: on the first wake-up, when they change, and every Nth wake-up
        self._count += 1
        try:
            rules = self.api.call("GET", "/api/rules/active", timeout=5)
        except Exception:
            rules = None
        if rules:
            every = rules.get("refresh_interval", 10)
            if (self._rules_epoch == 0 or rules["epoch"] != self._rules_epoch
                    or (every > 0 and self._count % every == 0)):
                if rules["rules"]:
                    prompt += "\n\nRULES:\n" + "; ".join(rules["rules"])
                self._rules_epoch = rules["epoch"]
                try:
                    self.api.call("POST", f"/api/rules/agent_sync/{self.name}",
                                  {"epoch": rules["epoch"]}, timeout=5)
                except Exception:
                    pass
        return prompt


class ActivityReporter:
    """Busy/idle pill. While busy it re-sends every 3 s; the server lease is 8 s."""

    def __init__(self, api: Api, stop: threading.Event):
        self.api = api
        self._busy = False
        self._changed = threading.Event()
        threading.Thread(target=self._loop, args=(stop,), daemon=True).start()

    def set(self, busy: bool):
        if busy != self._busy:
            self._busy = busy
            self._changed.set()

    def _loop(self, stop: threading.Event):
        while not stop.is_set():
            self._changed.wait(3)
            self._changed.clear()
            try:
                self.api.activity(self._busy)
            except Exception:
                pass


def _child_env(server: str) -> dict:
    """Environment for the CLI: bypass proxies for the chat server's host."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # launched from inside Claude Code: not a nested session
    host = urllib.parse.urlsplit(server).hostname or ""
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, "")
        if host and host not in current.split(","):
            env[key] = f"{current},{host}" if current else host
    return env


# ---------------------------------------------------------------------------
# Claude Code: channel wake bridge
# ---------------------------------------------------------------------------

_BRIDGE_INSTRUCTIONS = (
    "Events from this channel are agentchattr chat wake-ups: someone @mentioned you in a "
    "chat room shared with humans and other agents. When one arrives, follow it: read the "
    "room with the agentchattr MCP tools (chat_read) and answer in the chat with chat_send. "
    "People are reading the chat, not this terminal."
)


def run_wake_bridge():
    """Stdio MCP server started by Claude Code. Do not print to stdout."""
    server = os.environ.get("AGENTCHATTR_SERVER", "")
    token = os.environ.get("AGENTCHATTR_TOKEN", "")
    state_file = os.environ.get("AGENTCHATTR_STATE", "")
    api = Api(server, token)
    stop = threading.Event()
    out_lock = threading.Lock()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    def send(msg: dict):
        with out_lock:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    def start_polling():
        try:
            name = api.me()["name"]
        except Exception as exc:
            log(f"wake bridge cannot reach the chat server: {exc}")
            return
        builder = PromptBuilder(api, name)
        reporter = ActivityReporter(api, stop)

        def deliver(triggers):
            prompt = builder.build(triggers)
            last = triggers[-1]
            meta = {"room": str(last.get("channel", "general")), "sender": str(last.get("sender", ""))}
            if last.get("job_id") is not None:
                meta["job_id"] = str(last["job_id"])
            _write_state(state_file, "busy")
            send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                  "params": {"content": prompt, "meta": meta}})

        def watch_state():
            # Hooks (UserPromptSubmit / Stop) write busy/idle into the state file.
            while not stop.is_set():
                reporter.set(_read_state(state_file) == "busy")
                stop.wait(1)

        threading.Thread(target=watch_state, daemon=True).start()
        poll_triggers(api, deliver, stop)

    started = False
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion", "")
            version = requested if requested in _CHANNEL_PROTOCOLS else _CHANNEL_PROTOCOLS[0]
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": version,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": WAKE_SERVER, "version": "1.0"},
                "instructions": _BRIDGE_INSTRUCTIONS,
            }})
        elif method == "notifications/initialized":
            if not started:
                started = True
                threading.Thread(target=start_polling, daemon=True).start()
        elif msg_id is not None and method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif msg_id is not None and method:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})
    stop.set()


def _write_state(path: str, state: str):
    if path:
        try:
            Path(path).write_text(state, "utf-8")
        except OSError:
            pass


def _read_state(path: str) -> str:
    try:
        return Path(path).read_text("utf-8").strip()
    except OSError:
        return ""


def run_hook(state: str):
    """Claude Code hook: record busy/idle for the wake bridge. Must stay fast."""
    _write_state(os.environ.get("AGENTCHATTR_STATE", ""), state)


def _cmd_path(p: Path | str) -> str:
    """Quoted, forward-slash path: works in hook shells on Windows (bash or cmd) and Unix."""
    return '"' + str(p).replace("\\", "/") + '"'


def run_claude(api: Api, me: dict, cwd: Path, extra: list[str]) -> int:
    exe = shutil.which("claude")
    if not exe:
        log("'claude' is not on PATH. Install Claude Code first.")
        return 1
    state_dir = ROOT / "data" / "client" / me["name"]
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "activity"
    _write_state(str(state_file), "idle")

    client_env = {"AGENTCHATTR_SERVER": api.server, "AGENTCHATTR_TOKEN": api.token,
                  "AGENTCHATTR_STATE": str(state_file)}
    mcp_config = {"mcpServers": {
        "agentchattr": {"type": "http", "url": me["mcp_url"],
                        "headers": {"Authorization": f"Bearer {api.token}"}},
        WAKE_SERVER: {"type": "stdio", "command": sys.executable,
                      "args": [str(Path(__file__).resolve()), "wake-bridge"], "env": client_env},
    }}
    hook = lambda state: f"{_cmd_path(sys.executable)} {_cmd_path(Path(__file__).resolve())} hook {state}"
    settings = {
        # A wake-up arrives while nobody watches the terminal: the chat tools
        # themselves must not stop at a permission prompt. Other tools still ask.
        "permissions": {"allow": ["mcp__agentchattr"]},
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": hook("busy")}]}],
            "Stop": [{"hooks": [{"type": "command", "command": hook("idle")}]}],
        },
    }
    mcp_path = state_dir / "mcp.json"
    settings_path = state_dir / "settings.json"
    mcp_path.write_text(json.dumps(mcp_config, indent=2), "utf-8")
    settings_path.write_text(json.dumps(settings, indent=2), "utf-8")

    env = {**_child_env(api.server), **client_env}
    # Both list flags are variadic: keep them ahead of single-value flags and extra args.
    args = [exe, "--dangerously-load-development-channels", f"server:{WAKE_SERVER}",
            "--mcp-config", str(mcp_path), "--settings", str(settings_path), *extra]
    log(f"starting Claude Code as @{me['name']} in {cwd}")
    log("accept the development-channels prompt: it is how @mentions wake this session")
    return subprocess.call(args, cwd=str(cwd), env=env)


# ---------------------------------------------------------------------------
# Codex: shared app-server daemon + codex queue
# ---------------------------------------------------------------------------

def _codex_argv() -> list[str] | None:
    """Command prefix for Codex that avoids cmd.exe where possible.

    On Windows npm installs a codex.cmd shim; arguments passed through cmd.exe
    are re-parsed (quotes, %VAR%, &), so prefer `node <package>/bin/codex.js`.
    """
    exe = shutil.which("codex")
    if not exe:
        return None
    if exe.lower().endswith((".cmd", ".bat")):
        script = Path(exe).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, str(script)]
    return [exe]


def _shell_safe(text: str) -> str:
    """Last resort for a cmd.exe shim: drop characters cmd.exe would interpret."""
    for ch in '"%^&|<>':
        text = text.replace(ch, "'" if ch == '"' else " ")
    return text.replace("\r", " ").replace("\n", " ")


class CodexRpc:
    """JSON-RPC to Codex's shared app-server daemon via `codex app-server proxy`.

    The daemon's control socket speaks WebSocket. The proxy relays its bytes
    over stdio, which behaves the same on every platform, and the websockets
    sans-I/O protocol does the framing.
    """

    def __init__(self, argv: list[str], cwd: Path, env: dict):
        from websockets.client import ClientProtocol
        from websockets.uri import parse_uri

        self._proc = subprocess.Popen([*argv, "app-server", "proxy"], cwd=str(cwd), env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._proto = ClientProtocol(parse_uri("ws://localhost/"), max_size=None)
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 0
        self._open = threading.Event()
        self._closed = False
        self._fragments: list[bytes] = []
        with self._lock:
            self._proto.send_request(self._proto.connect())
            self._flush_locked()
        threading.Thread(target=self._reader, daemon=True).start()
        if not self._open.wait(10):
            self.close()
            raise RuntimeError("no answer from the Codex app-server daemon")
        self.call("initialize", {"clientInfo": {"name": "agentchattr", "version": "1.0"}})
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def _flush_locked(self):
        try:
            for data in self._proto.data_to_send():
                if data:
                    self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except OSError:
            self._closed = True

    def _reader(self):
        from websockets.frames import Frame, Opcode
        from websockets.http11 import Response

        while True:
            chunk = self._proc.stdout.read1(65536)
            with self._lock:
                if chunk:
                    self._proto.receive_data(chunk)
                else:
                    self._proto.receive_eof()
                events = self._proto.events_received()
                self._flush_locked()
            for ev in events:
                if isinstance(ev, Response):
                    if ev.status_code == 101:
                        self._open.set()
                elif isinstance(ev, Frame) and ev.opcode in (Opcode.TEXT, Opcode.CONT):
                    self._fragments.append(ev.data)
                    if ev.fin:
                        self._dispatch(b"".join(self._fragments))
                        self._fragments = []
            if not chunk:
                self._closed = True
                for q in list(self._pending.values()):
                    q.put({"error": {"message": "daemon connection closed"}})
                return

    def _dispatch(self, data: bytes):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            q = self._pending.pop(msg["id"], None)
            if q:
                q.put(msg)
        # Notifications are ignored: status is polled with thread/read.

    def _send(self, msg: dict):
        with self._lock:
            self._proto.send_text(json.dumps(msg).encode())
            self._flush_locked()

    def call(self, method: str, params: dict, timeout: float = 15):
        if self._closed:
            raise RuntimeError("daemon connection closed")
        q: queue.Queue = queue.Queue(1)
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = q
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"{method}: no answer from the daemon") from None
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def loaded_threads(self) -> list[str]:
        return list(self.call("thread/loaded/list", {}).get("data", []))

    def read_thread(self, thread_id: str) -> dict:
        return self.call("thread/read", {"threadId": thread_id}).get("thread", {})

    def close(self):
        self._closed = True
        try:
            self._proc.terminate()
        except OSError:
            pass


def _same_dir(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


class CodexLink:
    """Finds the TUI's thread on the daemon, reports busy/idle, delivers wake-ups."""

    def __init__(self, argv: list[str], cwd: Path, env: dict, reporter: ActivityReporter,
                 name: str, inject=None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.reporter, self.name, self.inject = reporter, name, inject
        self.rpc: CodexRpc | None = None
        self.thread_id = ""
        self._known: set[str] = set()
        self._linked = threading.Event()

    def connect(self) -> bool:
        started = subprocess.run([*self.argv, "app-server", "daemon", "start"], cwd=str(self.cwd),
                                 env=self.env, capture_output=True, text=True)
        if started.returncode != 0:
            log(f"codex app-server daemon did not start: {started.stderr.strip()[-300:]}")
            return False
        try:
            self.rpc = CodexRpc(self.argv, self.cwd, self.env)
            self._known = set(self.rpc.loaded_threads())  # threads that are not ours
        except Exception as exc:
            log(f"cannot talk to the Codex daemon: {exc}")
            self.rpc = None
            return False
        return True

    def watch(self, alive, stop: threading.Event):
        """Until the TUI exits: link its thread (and later /new threads), poll status."""
        while alive() and not stop.is_set():
            try:
                for tid in self.rpc.loaded_threads():
                    if tid in self._known:
                        continue
                    self._known.add(tid)
                    thread = self.rpc.read_thread(tid)
                    if thread.get("parentThreadId") or not _same_dir(thread.get("cwd", ""), self.cwd):
                        continue
                    self.thread_id = tid
                    self._linked.set()
                    log(f"linked Codex session {tid}")
                    try:
                        self.rpc.call("thread/name/set", {"threadId": tid, "name": f"agentchattr-{self.name}"})
                    except Exception:
                        pass
                if self.thread_id:
                    status = self.rpc.read_thread(self.thread_id).get("status", {})
                    self.reporter.set(status.get("type") == "active")
            except Exception as exc:
                if self.rpc and self.rpc._closed:
                    log(f"lost the Codex daemon connection ({exc})")
                    return
            stop.wait(1)

    def deliver(self, prompt: str):
        if not self._linked.wait(30):
            if self.inject:
                log("no Codex daemon session linked; typing the wake-up into the console")
                self.inject(prompt.replace("\n", " "))
            else:
                log("no Codex daemon session linked yet; wake-up dropped")
            return
        via_cmd_shim = self.argv[0].lower().endswith((".cmd", ".bat"))
        message = _shell_safe(prompt) if via_cmd_shim else prompt
        result = subprocess.run([*self.argv, "queue", "--thread", self.thread_id,
                                 "--message", message, "--resume"],
                                cwd=str(self.cwd), env=self.env, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"codex queue failed: {(result.stderr or result.stdout).strip()[-300:]}")
        else:
            self.reporter.set(True)


def run_codex(api: Api, me: dict, cwd: Path, extra: list[str]) -> int:
    argv = _codex_argv()
    if not argv:
        log("'codex' is not on PATH. Install Codex first (npm install -g @openai/codex).")
        return 1
    stop = threading.Event()
    env = _child_env(api.server)
    reporter = ActivityReporter(api, stop)

    inject = None
    if sys.platform == "win32":
        # Codex's TUI needs VT processing on the console; the console injector
        # is the fallback when the daemon path is not available on this machine.
        from wrapper_windows import _vt_keepalive_thread, enable_vt_mode, inject as win_inject
        enable_vt_mode()
        _vt_keepalive_thread()
        inject = win_inject

    link = CodexLink(argv, cwd, env, reporter, me["name"], inject=inject)
    if not link.connect() and not inject:
        log("without the Codex daemon this client cannot wake Codex on this platform")

    mcp_flags = [
        "-c", f'mcp_servers.agentchattr.url="{me["mcp_url"]}"',
        "-c", f'mcp_servers.agentchattr.http_headers={{Authorization="Bearer {api.token}"}}',
        "-c", 'mcp_servers.agentchattr.default_tools_approval_mode="approve"',
    ]
    log(f"starting Codex as @{me['name']} in {cwd}")
    tui = subprocess.Popen([*argv, *mcp_flags, *extra], cwd=str(cwd), env=env)
    if link.rpc:
        threading.Thread(target=link.watch, args=(lambda: tui.poll() is None, stop), daemon=True).start()

    builder = PromptBuilder(api, me["name"])
    threading.Thread(target=poll_triggers,
                     args=(api, lambda batch: link.deliver(builder.build(batch)), stop),
                     daemon=True).start()
    try:
        return tui.wait()
    finally:
        stop.set()
        if link.rpc:
            link.rpc.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_profile(name: str) -> dict:
    path = ROOT / "client.local.toml"
    if not path.exists():
        raise SystemExit(f"Error: {path} not found (copy client.local.toml.example).")
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"Error: {path} is not valid TOML: {e}\n"
            "Hint: Windows paths need forward slashes (\"C:/work/repo\") or "
            "single quotes ('C:\\work\\repo'); backslashes in double quotes are escapes."
        )
    profile = cfg.get("profiles", {}).get(name)
    if profile is None:
        raise SystemExit(f"Error: no [profiles.{name}] in {path}.")
    return {"server": cfg.get("server", ""), **profile}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "wake-bridge":
        run_wake_bridge()
        return
    if len(sys.argv) > 2 and sys.argv[1] == "hook":
        run_hook(sys.argv[2])
        return

    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    parser = argparse.ArgumentParser(description="Join Claude Code or Codex to an agentchattr server.")
    parser.add_argument("profile", nargs="?", help="profile name in client.local.toml")
    parser.add_argument("--server", help="chat server URL, e.g. http://chat-server:8300")
    parser.add_argument("--token", help="client token from `python tokens.py add`")
    parser.add_argument("--cwd", help="working directory for the agent")
    args = parser.parse_args(argv)

    profile = _load_profile(args.profile) if args.profile else {}
    server = args.server or os.environ.get("AGENTCHATTR_SERVER") or profile.get("server", "")
    token = args.token or os.environ.get("AGENTCHATTR_TOKEN") or profile.get("token", "")
    cwd = Path(args.cwd or profile.get("cwd") or ".").expanduser().resolve()
    extra = [*profile.get("args", []), *extra]
    if not server or not token:
        parser.error("server and token are required (flags, AGENTCHATTR_SERVER/TOKEN, or a profile)")

    api = Api(server, token)
    try:
        me = api.me(announce=True)
    except AuthError as exc:
        raise SystemExit(f"Error: {exc}.")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Error: cannot reach {server}: {exc}")
    log(f"connected to {api.server} as @{me['name']} ({me['kind']})")

    runner = {"claude": run_claude, "codex": run_codex}.get(me["kind"])
    if runner is None:
        raise SystemExit(f"Error: token kind '{me['kind']}' is not an agent (claude or codex).")
    # Ctrl+C belongs to the CLI (interrupt a turn); this process stays until it exits.
    # A no-op handler rather than SIG_IGN, which POSIX children would inherit.
    signal.signal(signal.SIGINT, lambda *_: None)
    try:
        code = runner(api, me, cwd, extra)
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
