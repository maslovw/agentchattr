#!/usr/bin/env sh
# agentchattr - joins Claude Code or Codex on this machine to a chat server on the LAN.
# Usage: sh join.sh <profile>     (profiles live in client.local.toml)
# No tmux, no local server: the token decides the identity and which CLI starts.
cd "$(dirname "$0")/.."

if [ -z "$1" ]; then
    echo "Usage: sh join.sh <profile>  (profiles are defined in client.local.toml)"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv || exit 1
    .venv/bin/python -m pip install -q -r requirements.txt || exit 1
fi

exec .venv/bin/python client.py "$@"
