"""Per-client access tokens for machines on the LAN.

Every human browser and every remote Claude Code / Codex client gets its own
token, issued here by the server admin. The token is the identity: its name is
the chat sender and the @mention handle. Only SHA-256 digests are stored, so
the raw token is shown once, when it is created.

Usage:
    python tokens.py add maslov --kind human
    python tokens.py add claude-pc1 --kind claude
    python tokens.py add codex-pc1 --kind codex
    python tokens.py list
    python tokens.py revoke codex-pc1

The server re-reads data/tokens.json when it changes, so tokens can be added
or revoked while it is running.
"""

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

KINDS = ("human", "claude", "codex")
DEFAULT_COLORS = {"human": "#9aa0a6", "claude": "#da7756", "codex": "#10a37f"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_RESERVED = {"system", "user", "all", "both", "unknown"}


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def is_local_request(client_host: str, host_header: str) -> bool:
    """True for a request from this machine that is addressed to a loopback name.

    Only these keep the token-free legacy path. Checking the Host header as well
    keeps DNS-rebinding pages (Host: evil.example resolving to 127.0.0.1) out.
    """
    if client_host not in _LOOPBACK_CLIENTS:
        return False
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[: host.find("]") + 1]
    else:
        host = host.split(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def name_conflict(name: str, agent_bases) -> str | None:
    """Return why `name` can't be a token identity, or None if it can.

    Names must not shadow a config.toml agent family (`claude`, `claude-2`):
    those belong to the local wrappers and their slot numbering.
    """
    if not _NAME_RE.match(name):
        return "use 1-32 lowercase letters, digits or '-', starting with a letter or digit"
    if name in _RESERVED:
        return f"'{name}' is reserved"
    bases = set(agent_bases)
    if name in bases:
        return f"'{name}' is a config.toml agent name"
    prefix, _, suffix = name.rpartition("-")
    if prefix in bases and suffix.isdigit():
        return f"'{name}' looks like a slot of the config.toml agent '{prefix}'"
    return None


class TokenStore:
    def __init__(self, path, reserved_bases=()):
        self._path = Path(path)
        self._reserved_bases = set(reserved_bases)
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._by_digest: dict[str, str] = {}
        self._mtime = None
        self.reload_if_changed()

    def reload_if_changed(self) -> bool:
        """Re-read the file if it changed on disk. Returns True when it did."""
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        with self._lock:
            if mtime == self._mtime:
                return False
            clients = {}
            if mtime is not None:
                try:
                    clients = json.loads(self._path.read_text("utf-8")).get("clients", {})
                except (OSError, ValueError):
                    return False  # keep the last good copy; retry on the next change
            self._mtime = mtime
            self._clients = clients
            self._by_digest = {c["token_sha256"]: n for n, c in clients.items()}
        return True

    def _save_locked(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": self._clients}, indent=2) + "\n", "utf-8")
        tmp.replace(self._path)
        self._by_digest = {c["token_sha256"]: n for n, c in self._clients.items()}
        self._mtime = self._path.stat().st_mtime_ns

    def add(self, name: str, kind: str, label: str | None = None, color: str | None = None) -> str:
        """Create a client and return its raw token. Raises ValueError."""
        self.reload_if_changed()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        problem = name_conflict(name, self._reserved_bases)
        if problem:
            raise ValueError(problem)
        token = secrets.token_urlsafe(24)
        with self._lock:
            if name in self._clients:
                raise ValueError(f"'{name}' already exists (revoke it first)")
            self._clients[name] = {
                "kind": kind,
                "label": label or name,
                "color": color or DEFAULT_COLORS[kind],
                "token_sha256": token_digest(token),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_locked()
        return token

    def revoke(self, name: str) -> bool:
        self.reload_if_changed()
        with self._lock:
            if self._clients.pop(name, None) is None:
                return False
            self._save_locked()
        return True

    def resolve(self, token: str) -> dict | None:
        """Map a raw token to {name, kind, label, color}, or None."""
        if not token:
            return None
        self.reload_if_changed()
        digest = token_digest(token)
        with self._lock:
            name = self._by_digest.get(digest)
            if name is None:
                return None
            record = self._clients[name]
            if not hmac.compare_digest(record["token_sha256"], digest):
                return None
            return _public(name, record)

    def list(self) -> list[dict]:
        self.reload_if_changed()
        with self._lock:
            return [_public(n, c) for n, c in sorted(self._clients.items())]

    def agents(self) -> dict[str, dict]:
        """Agent clients as registry base configs: {name: {label, color, kind, token_sha256}}."""
        self.reload_if_changed()
        with self._lock:
            return {
                n: {"label": c["label"], "color": c["color"], "kind": c["kind"],
                    "token_sha256": c["token_sha256"]}
                for n, c in self._clients.items() if c["kind"] != "human"
            }

    def names(self) -> set[str]:
        with self._lock:
            return set(self._clients)


def _public(name: str, record: dict) -> dict:
    return {"name": name, "kind": record["kind"], "label": record["label"],
            "color": record["color"], "created": record.get("created", "")}


def main():
    import argparse

    from config_loader import apply_cli_overrides, load_config

    parser = argparse.ArgumentParser(description="Manage agentchattr client tokens.")
    parser.add_argument("--data-dir", default=None, help="Override server.data_dir (path)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="Create a client token (printed once)")
    add.add_argument("name")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--label", default=None)
    add.add_argument("--color", default=None)
    sub.add_parser("list", help="List clients")
    rev = sub.add_parser("revoke", help="Revoke a client token")
    rev.add_argument("name")
    args = parser.parse_args()

    apply_cli_overrides()
    root = Path(__file__).parent
    config = load_config(root)
    data_dir = root / config.get("server", {}).get("data_dir", "./data")
    store = TokenStore(data_dir / "tokens.json", reserved_bases=config.get("agents", {}).keys())

    if args.cmd == "add":
        try:
            token = store.add(args.name, args.kind, args.label, args.color)
        except ValueError as exc:
            raise SystemExit(f"Error: {exc}")
        print(f"Created {args.kind} client '{args.name}'. Token (shown once):\n\n  {token}\n")
        if args.kind == "human":
            print("Open http://<server>:<port>/ and paste the token on the login page.")
        else:
            print("Put it in client.local.toml on that machine (see client.local.toml.example).")
    elif args.cmd == "list":
        for c in store.list():
            print(f"  {c['name']:<24} {c['kind']:<7} {c['created']}")
    elif args.cmd == "revoke":
        if not store.revoke(args.name):
            raise SystemExit(f"Error: no client named '{args.name}'")
        print(f"Revoked '{args.name}'.")


if __name__ == "__main__":
    main()
