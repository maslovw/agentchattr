"""client.py contracts that the e2e run depends on but no CLI checks for us."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
