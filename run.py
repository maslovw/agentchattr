"""Entry point — starts MCP server (port 8200) + web UI (port 8300)."""

import argparse
import asyncio
import secrets
import sys
import threading
import time
import logging
from pathlib import Path

# Ensure the project directory is on the import path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Start agentchattr (web UI + MCP server).",
        epilog="Flags override config.toml for this invocation. The same flags "
               "are also accepted by wrapper.py and wrapper_api.py so a launcher "
               "can isolate per-project instances by passing matching values to "
               "each process.",
    )
    parser.add_argument("--data-dir",      default=None, help="Override server.data_dir (path)")
    parser.add_argument("--port",          default=None, help="Override server.port (int)")
    parser.add_argument("--mcp-http-port", default=None, help="Override mcp.http_port (int)")
    parser.add_argument("--mcp-sse-port",  default=None, help="Override mcp.sse_port (int)")
    parser.add_argument("--upload-dir",    default=None, help="Override images.upload_dir (path)")
    parser.add_argument("--allow-network", action="store_true",
                        help="Allow binding to non-localhost hosts (with confirmation).")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse flags for --help support; the actual env propagation happens via
    # the shared config_loader.apply_cli_overrides helper so run.py and the
    # wrappers use identical extraction logic.
    _parse_args()

    from config_loader import apply_cli_overrides, load_config
    apply_cli_overrides()

    config_path = ROOT / "config.toml"
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    config = load_config(ROOT)

    # --- Security: generate a random session token (in-memory only) ---
    session_token = secrets.token_hex(32)

    # Configure the FastAPI app (creates shared store)
    from app import app, configure, set_event_loop, store as _store_ref
    configure(config, session_token=session_token)

    # Share stores with the MCP bridge
    from app import store, rules, summaries, jobs, room_settings, registry, router as app_router, agents as app_agents, session_engine, session_store
    import mcp_bridge
    mcp_bridge.store = store
    mcp_bridge.rules = rules
    mcp_bridge.summaries = summaries
    mcp_bridge.jobs = jobs
    mcp_bridge.room_settings = room_settings
    mcp_bridge.registry = registry
    mcp_bridge.config = config
    mcp_bridge.router = app_router
    mcp_bridge.agents = app_agents

    # Enable cursor and role persistence across restarts
    data_dir = ROOT / config.get("server", {}).get("data_dir", "./data")
    mcp_bridge._CURSORS_FILE = data_dir / "mcp_cursors.json"
    mcp_bridge._load_cursors()
    mcp_bridge._ROLES_FILE = data_dir / "roles.json"
    mcp_bridge._load_roles()

    # Start MCP servers in background threads
    host = config.get("server", {}).get("host", "127.0.0.1")
    http_port = config.get("mcp", {}).get("http_port", 8200)
    sse_port = config.get("mcp", {}).get("sse_port", 8201)
    mcp_bridge.mcp_http.settings.port = http_port
    mcp_bridge.mcp_sse.settings.port = sse_port
    mcp_bridge.configure_network(host)

    threading.Thread(target=mcp_bridge.run_http_server, daemon=True).start()
    threading.Thread(target=mcp_bridge.run_sse_server, daemon=True).start()
    time.sleep(0.5)
    logging.getLogger(__name__).info("MCP streamable-http on port %d, SSE on port %d", http_port, sse_port)

    # Mount static files
    from fastapi.staticfiles import StaticFiles
    from fastapi import Form
    from fastapi.requests import Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    import json as _json
    import app as app_module
    from tokens import is_local_request

    static_dir = ROOT / "static"

    @app.post("/login")
    async def login(token: str = Form("")):
        # Humans on other machines sign in with their own token (tokens.py).
        token = token.strip()
        if not app_module.human_for_token(token):
            return HTMLResponse(_login_page("Unknown or revoked token."), status_code=403)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("agentchattr_token", token, httponly=True, samesite="strict",
                        max_age=180 * 24 * 3600)
        return resp

    @app.get("/")
    async def index(request: Request):
        cookie_token = request.cookies.get("agentchattr_token", "")
        human = app_module.human_for_token(cookie_token)
        if human:
            page_token, page_name = cookie_token, human
        elif is_local_request(request.client.host if request.client else "",
                              request.headers.get("host", "")):
            # This machine, addressed by a loopback name: the legacy single-user
            # session token. Same-origin policy keeps other pages from reading it,
            # and the Host check keeps DNS-rebinding pages out.
            page_token, page_name = session_token, ""
        else:
            return HTMLResponse(_login_page(), headers={"Cache-Control": "no-store"})

        # Read index.html fresh each request so changes take effect without restart.
        html = (static_dir / "index.html").read_text("utf-8")
        injected = html.replace(
            "</head>",
            f"<script>window.__SESSION_TOKEN__={_json.dumps(page_token)};"
            f"window.__HUMAN_NAME__={_json.dumps(page_name)};</script>\n</head>",
        )
        return HTMLResponse(injected, headers={"Cache-Control": "no-store"})

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("agentchattr_token")
        return resp

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Capture the event loop for the store→WebSocket bridge
    @app.on_event("startup")
    async def on_startup():
        set_event_loop(asyncio.get_running_loop())
        # Resume any sessions that were active before restart
        if session_engine:
            session_engine.resume_active_sessions()

    # Run web server
    import uvicorn
    port = config.get("server", {}).get("port", 8300)

    # --- Security: warn if binding to a non-localhost address ---
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\n  !! SECURITY WARNING — binding to {host} !!")
        print("  This exposes agentchattr (web UI and MCP) to your local network.")
        print("  Other machines need a per-client token: python tokens.py add <name> --kind human|claude|codex")
        print()
        print("  Risks:")
        print("  - No TLS: traffic (including tokens) is plaintext and can be sniffed")
        print("  - A token holder can @mention agents and trigger tool execution")
        print("  - If agents run with auto-approve, this means remote code execution")
        print()
        print("  Only use this on a trusted network. Never on public/shared WiFi.")
        if "--allow-network" not in sys.argv:
            print("  Pass --allow-network to start anyway, or set host to 127.0.0.1.\n")
            sys.exit(1)
        print()

    print(f"\n  agentchattr")
    print(f"  Web UI:  http://{host}:{port}")
    print(f"  MCP HTTP: http://{host}:{http_port}/mcp  (Claude, Codex)")
    print(f"  MCP SSE:  http://{host}:{sse_port}/sse   (Gemini)")
    print(f"  Data:    {data_dir}")
    print(f"  Agents auto-trigger on @mention")
    print(f"\n  Session token: {session_token}\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


_LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>agentchattr - sign in</title>
<style>
body {{ background:#0f0f17; color:#e6e6eb; font-family:system-ui,sans-serif;
       display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
form {{ background:#181824; padding:28px; border-radius:10px; width:340px; }}
h1 {{ font-size:18px; margin:0 0 6px; }} p {{ color:#9aa0a6; font-size:13px; margin:0 0 16px; }}
input {{ width:100%; box-sizing:border-box; padding:9px; border-radius:6px; border:1px solid #333;
        background:#0f0f17; color:inherit; font-family:monospace; }}
button {{ margin-top:12px; width:100%; padding:9px; border:0; border-radius:6px;
         background:#7c6af2; color:#fff; font-weight:600; cursor:pointer; }}
.err {{ color:#ff6b6b; }}
</style></head>
<body><form method="post" action="/login">
<h1>agentchattr</h1><p>Paste the token the server admin created for you
(<code>python tokens.py add NAME --kind human</code>).</p>{error}
<input name="token" type="password" placeholder="token" autofocus required>
<button type="submit">Sign in</button>
</form></body></html>"""


def _login_page(error: str = "") -> str:
    return _LOGIN_HTML.format(error=f'<p class="err">{error}</p>' if error else "")


if __name__ == "__main__":
    main()

