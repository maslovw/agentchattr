# <img src="static/logo.png" alt="" width="32"> agentchattr (LAN fork)

![Windows](https://img.shields.io/badge/platform-Windows-blue) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey) ![Linux](https://img.shields.io/badge/platform-Linux-orange) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-green)

A chat server where humans and AI coding agents work together in shared rooms. This fork of [bcurts/agentchattr](https://github.com/bcurts/agentchattr) adds **LAN mode**: one server on the network, and **Claude Code** or **Codex** sessions on other machines (Windows included) join it, each with its own token. There is no tmux and no keystroke injection: an @mention wakes the agent through the CLI's own interfaces, even when the session is idle.

The upstream single-machine mode (tmux on Mac/Linux, console injection on Windows, all upstream providers) still works unchanged. See [Single-machine mode](#single-machine-mode).

![screenshot](screenshot.png)

## How LAN mode works

- Every human and every agent has a personal token, created on the server with `tokens.py`. The token name is the chat identity and the @mention handle. Nobody can post under another name.
- Agent machines run `client.py` (through `windows\join.bat` or `macos-linux/join.sh`). It starts the CLI with the chat MCP tools configured and long-polls the server for @mentions.
- **Claude Code**: `client.py` adds a small channel server to the session (stdio MCP with the `claude/channel` capability). Each @mention arrives as a channel event and starts a turn. Busy/idle comes from `UserPromptSubmit` and `Stop` hooks.
- **Codex**: the TUI runs on Codex's shared app-server daemon. `client.py` finds the TUI's thread there, reports busy/idle from the thread status, and delivers each @mention with `codex queue --thread <id> --resume`. If the daemon is not available on Windows, it falls back to typing into the console.
- Every channel is a room. A room can have a member list, and then only its members are woken.

```plantuml
@startuml
actor Human
node "Server" {
  [Web UI + agent API :8300] as UI
  [MCP :8200] as MCP
}
node "PC A" {
  [claude] as C
  [wake bridge\n(client.py, stdio MCP)] as W
}
node "PC B" {
  [codex TUI] as X
  [codex daemon] as D
  [client.py] as K
}
Human --> UI : browser, personal token
C --> MCP : chat tools, Bearer token
C --> W : starts
W --> UI : long-poll /api/agent/wait
W --> C : channel event (wake)
X --> MCP : chat tools, Bearer token
K --> UI : long-poll /api/agent/wait
K --> D : codex queue --resume
@enduml
```

```
You type "@claude-pc1 review the parser change" in #backend
  -> the server routes the mention (only to room members, if the room has a list)
  -> client.py on PC A receives it from /api/agent/wait
  -> the wake bridge sends a channel event and Claude starts a turn
  -> Claude reads and answers with chat_read / chat_send over MCP, using its token
  -> if Claude @mentions @codex-pc2, client.py on PC B runs codex queue and Codex answers
```

## Quickstart: LAN mode

**Requirements**

- Python 3.11 or newer on every machine (the code uses `tomllib`).
- Claude Code logged in with a claude.ai account. Channels don't work with API-key login. On Team/Enterprise plans an admin must enable channels (`channelsEnabled`).
- Codex CLI 0.166 or newer (`codex app-server daemon`, `codex queue`), logged in.
- All machines on one trusted network. Clients must reach the server on TCP 8300 and 8200.

### 1. Start the server

```bash
git clone -b lan-mode https://github.com/maslovw/agentchattr.git
cd agentchattr
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
```

In `config.toml`, bind to the network:

```toml
[server]
host = "0.0.0.0"
```

Start it. Binding off localhost requires `--allow-network`:

```bash
.venv/bin/python run.py --allow-network       # Windows: .venv\Scripts\python
```

Open TCP 8300 (web UI and agent API) and 8200 (MCP) in the firewall. On a Windows server, from an admin cmd:

```
netsh advfirewall firewall add rule name=agentchattr dir=in action=allow protocol=TCP localport=8300,8200
```

### 2. Create tokens

```bash
.venv/bin/python tokens.py add alice --kind human
.venv/bin/python tokens.py add claude-pc1 --kind claude
.venv/bin/python tokens.py add codex-pc2 --kind codex
.venv/bin/python tokens.py list
.venv/bin/python tokens.py revoke codex-pc2
```

- Each token is printed once. Only its SHA-256 digest is stored (`data/tokens.json`), so to replace a lost token, revoke it and add it again.
- The running server picks up changes within a few seconds. A revoked agent stops receiving mentions.
- The kind decides what the token is for: `human` signs in to the browser, `claude` and `codex` decide which CLI `client.py` starts.
- Names use lowercase letters, digits and `-` (up to 32 characters). They can't reuse an agent name from `config.toml` (`claude`, `codex`, ...), its numbered variants (`claude-2`), or `system`, `user`, `all`, `both`, `unknown`.
- `--label "Display name"` and `--color "#hex"` are optional.

### 3. Join agents

On each agent machine:

```
git clone -b lan-mode https://github.com/maslovw/agentchattr.git
cd agentchattr
copy client.local.toml.example client.local.toml      # Mac/Linux: cp
```

Edit `client.local.toml` (it is gitignored):

```toml
server = "http://192.168.1.10:8300"

[profiles.claude-pc1]
token = "paste-the-token-here"
cwd = "C:/work/myrepo"            # forward slashes, or single quotes for backslashes
# args = ["--model", "opus"]     # extra CLI arguments

[profiles.codex-pc2]
token = "paste-the-token-here"
cwd = "C:/work/myrepo"
# args = ["--dangerously-bypass-approvals-and-sandbox"]
```

Start one agent per terminal window. Run the launcher from a cmd window rather than double-clicking it, because it needs the profile name:

```
windows\join.bat claude-pc1
sh macos-linux/join.sh codex-pc2
```

The first run creates `.venv` and installs the dependencies. Arguments after `--` go to the CLI, for example `windows\join.bat claude-pc1 -- --model opus`. Without a profile, use `python client.py --server URL --token TOKEN --cwd DIR`, or set `AGENTCHATTR_SERVER` and `AGENTCHATTR_TOKEN`.

What to expect:

- `[agentchattr] connected to http://...:8300 as @claude-pc1 (claude)`, and a join message in the chat.
- **Claude** asks you to trust the folder, then shows "Loading development channels". Choose "I am using this for local development". It asks on every launch, because the wake bridge is not on Anthropic's channel allowlist.
- **Codex** logs `[agentchattr] linked Codex session <id>` once the TUI is up. `codex app-server daemon did not start` means wake-ups are typed into the console instead.

The chat tools (`mcp__agentchattr`) are pre-approved for agents started this way. Every other tool still asks in the agent's terminal. Mentions sent while an agent is offline wait for it (up to 20).

### 4. Join as a human

Open `http://<server>:8300/` and sign in with a human token. The browser keeps it in a cookie for 180 days, and `/logout` signs out. Your chat name comes from the token. On the server machine itself, `http://localhost:8300` works without signing in, as in upstream.

### 5. Rooms

Every channel is a room. Click **+ Members** above the input to choose which agents belong to the current room.

- A room with members wakes only its members. @mentioning anyone else posts "Not a member of #room".
- A room without members is open to every agent.
- The member list follows a channel rename and is removed when the channel is deleted.

### Check that it works

1. Both agents show as online.
2. `@claude-pc1 reply pong` and `@codex-pc2 reply pong` get an answer, and the status pill spins while the agent works.
3. Mention an agent again after it has been idle for a while. It still wakes.
4. In a room limited to `claude-pc1`, `@codex-pc2` gives the "Not a member" notice.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'tomllib'` | Python is older than 3.11. Recreate `.venv` with 3.11 or newer. |
| `python3.12 -m venv` fails in `ensurepip` (Debian/Ubuntu) | `sudo apt install python3.12-venv`, or `python3.12 -m venv --without-pip .venv` and then `curl -sS https://bootstrap.pypa.io/get-pip.py \| .venv/bin/python`. |
| The server refuses to start on `0.0.0.0` | Add `--allow-network`. |
| Client: `cannot reach http://...` | Check the IP and the firewall on the server (8300 and 8200). Open the URL in a browser on the client machine. |
| Client: unknown or revoked token | Check `tokens.py list` on the server and create a new token. |
| Client: `client.local.toml is not valid TOML` | Write Windows paths as `"C:/work/repo"` or `'C:\work\repo'`. |
| Claude never wakes | Accept the "Loading development channels" prompt. Channels need a claude.ai login, and Team/Enterprise plans need `channelsEnabled`. |
| Claude answers but its pill never spins | The busy/idle hooks did not run. Check the `[agentchattr]` lines in the agent window. |
| Codex: `codex app-server daemon did not start` | Codex is older than 0.166, or the daemon is not available on this machine. On Windows the client falls back to typing into the console. |
| The server's own browser shows the sign-in page | Only `localhost` and `127.0.0.1` skip sign-in. Any other host name needs a token (DNS rebinding guard). |

### Limitations

- Traffic is plain HTTP, so tokens can be sniffed. Use a trusted network only.
- Remote agents can't use `chat_send(image_path=...)`, because the path must exist on the server. They read attachments with `chat_attachment(url)`.
- Only Claude Code and Codex have LAN clients. Other providers use single-machine mode.
- The Codex daemon path and the Claude hooks are verified on Linux, not yet on Windows.

## Single-machine mode

The upstream setup: the server and all agents run on one machine, and the wrapper types each wake-up prompt into the agent's terminal (tmux on Mac/Linux, console injection on Windows). It supports every upstream provider.

### Quickstart (Windows)

**1. Open the `windows` folder and double-click a launcher** to start your agent - e.g. `start_claude.bat`, `start_codex.bat`, `start_gemini.bat`, etc.

On first launch, the script auto-creates a virtual environment, installs Python dependencies, and configures MCP. Each agent launcher auto-starts the server if one isn't already running, so you can launch in any order. Run multiple launchers for multiple agents - they share the same server.

<details>
<summary>All agent launchers (click to expand)</summary>

- `start.bat` - starts the chat server only
- `start_claude.bat` - starts Claude
- `start_codex.bat` - starts Codex
- `start_gemini.bat` - starts Gemini
- `start_copilot.bat` - starts GitHub Copilot CLI (requires `npm install -g @github/copilot`)
- `start_kimi.bat` - starts Kimi
- `start_qwen.bat` - starts Qwen
- `start_kilo.bat` - starts Kilo
- `start_kilo.bat provider/model` - starts Kilo with a specific model (e.g. `start_kilo.bat anthropic/claude-sonnet-4-20250514`)
- `start_codebuddy.bat` - starts CodeBuddy (first launch prompts interactive login)
- `start_minimax.bat` - starts MiniMax (requires `MINIMAX_API_KEY` env var)

**Auto-approve variants** (agents run tools without asking permission):

- `start_claude_skip-permissions.bat` - Claude with `--dangerously-skip-permissions`
- `start_codex_bypass.bat` - Codex with `--dangerously-bypass-approvals-and-sandbox`
- `start_gemini_yolo.bat` - Gemini with `--yolo`
- `start_qwen_yolo.bat` - Qwen with `--yolo`

</details>

**2. Open the chat:** Go to **http://localhost:8300** in your browser, or double-click `open_chat.html`.

**3. Talk to your agents:** Type `@claude`, `@codex`, `@gemini`, `@copilot`, `@kimi`, `@qwen`, `@kilo`, `@codebuddy`, or `@minimax` in your message, or use the toggle buttons above the input. The agent will wake up, read the chat, and respond.

> **Tip:** To manually prompt an agent to check chat, type `mcp read #general` in their terminal.

### Quickstart (Mac / Linux)

**1. Make sure tmux is installed:**

```bash
brew install tmux    # macOS
# apt install tmux   # Ubuntu/Debian
```

**2. Launch an agent:**

Open a terminal in the `macos-linux` folder (right-click → "Open Terminal Here", or `cd` into it) and run a launcher - e.g. `sh start_claude.sh`, `sh start_codex.sh`, `sh start_gemini.sh`, etc.

On first launch, the script auto-creates a virtual environment, installs Python dependencies, and configures MCP. Each agent launcher auto-starts the server in a separate terminal window if one isn't already running. The agent opens inside a **tmux** session. Detach with `Ctrl+B, D` - the agent keeps running in the background. Reattach with `tmux attach -t agentchattr-claude`.

<details>
<summary>All agent launchers (click to expand)</summary>

- `sh start.sh` - starts the chat server only
- `sh start_claude.sh` - starts Claude
- `sh start_codex.sh` - starts Codex
- `sh start_gemini.sh` - starts Gemini
- `sh start_copilot.sh` - starts GitHub Copilot CLI (requires `npm install -g @github/copilot`)
- `sh start_kimi.sh` - starts Kimi
- `sh start_qwen.sh` - starts Qwen
- `sh start_kilo.sh` - starts Kilo
- `sh start_kilo.sh provider/model` - starts Kilo with a specific model (e.g. `sh start_kilo.sh anthropic/claude-sonnet-4-20250514`)
- `sh start_codebuddy.sh` - starts CodeBuddy (first launch prompts interactive login)
- `sh start_minimax.sh` - starts MiniMax (requires `MINIMAX_API_KEY` env var)

**Auto-approve variants** (agents run tools without asking permission):

- `start_claude_skip-permissions.sh` - Claude with `--dangerously-skip-permissions`
- `start_codex_bypass.sh` - Codex with `--dangerously-bypass-approvals-and-sandbox`
- `start_gemini_yolo.sh` - Gemini with `--yolo`
- `start_qwen_yolo.sh` - Qwen with `--yolo`

</details>

**3. Open the chat:** Go to **http://localhost:8300** or open `open_chat.html`.

**4. Talk to your agents:** Type `@claude`, `@codex`, `@gemini`, `@copilot`, `@kimi`, `@qwen`, `@kilo`, `@codebuddy`, or `@minimax` in your message, or use the toggle buttons above the input. The agent will wake up, read the chat, and respond.


### How it works (single-machine)

```
You type "@claude what's the status on the renderer?"
  → server detects the @mention
  → wrapper injects "use mcp to read #general - you're mentioned..." into Claude's terminal
  → Claude reads recent messages, sees your question, responds in the channel
  → If Claude @mentions @codex, the same happens in Codex's terminal
  → Agents go back and forth until the loop guard pauses for your review

No copy-pasting between terminals. No manual prompting.
Agents wake each other up, coordinate, and report back.
```

<p align="center">
  <img src="gang.gif" alt="agentchattr gang" width="600"><br>
  <sub>the gang after <code>/hatmaking</code></sub>
</p>

## Features

### Agent-to-agent communication
Agents @mention each other and the server auto-triggers the target. Claude can wake Codex, Codex can respond back, Gemini can jump in - all autonomously. A per-channel loop guard pauses after N hops to prevent runaway conversations - a busy channel won't block other channels. Human @mentions always pass through, even when the loop guard is active. Type `/continue` to resume.

### Channels
Conversations are organized into channels (like Slack). The default channel is `#general`. Create new channels by clicking the `+` button in the channel bar, rename or delete them by clicking the active tab to reveal edit controls. Channels persist across server restarts. Each channel is also a room with an optional member list (see [Rooms](#5-rooms)).

Agents interact with channels via MCP: `chat_send(channel="debug")`, `chat_read(channel="debug")`. Omitting the channel parameter in `chat_read` returns messages from all channels. The `chat_channels` tool lets agents discover available channels.

When agents are triggered by an @mention, the wrapper injects `use mcp to read #channel-name - you're mentioned, take appropriate action and respond` so the agent reads the right channel automatically. Join/leave messages are broadcast to all channels so agents always see presence changes regardless of which channel they're monitoring.

### Jobs
Bounded work conversations - like Slack threads with status tracking. When a task comes up in chat, click **convert to job** on any message - the agent who wrote it will automatically reformat their message into a job proposal for you to Accept or Dismiss. You can also create jobs manually from the jobs panel. Jobs have a title, status (To Do → Active → Closed), and their own message thread.

When an agent is triggered with a job, it sees the full job context - title, status, and conversation history - so it can pick up exactly where the last agent left off. Jobs are visible regardless of which channel you're in.

Agents can also propose jobs directly via `chat_propose_job` - a proposal card appears in the timeline for you to Accept or Dismiss. The jobs panel opens from the header. Drag cards to reorder within a status group, click a card to open its conversation.

### Agent roles
Assign roles to agents to steer their behavior - Planner, Builder, Reviewer, Researcher, or any custom role. Roles aren't a hard constraint - they're a persistent nudge. The wrapper appends their role to the prompt injected into their terminal. The agent sees this every time it wakes up, shaping how it approaches the task.

Set roles from two places: click a **status pill** in the header bar to open a popover with rename + role picker, or click the **role pill** in any message header. Choose from presets or type a custom role (max 20 characters). Custom roles are saved and appear in both pickers - hover a custom role to reveal a trash icon for deletion. Roles are global per agent (not per-channel), persist across server restarts, and update instantly across all messages. Clear a role by selecting "None".

### Rules
Rules set the working style for your agents. Agents can propose rules via MCP (`chat_rules(action='propose')`), or you can add one directly from the Rules panel with `+`. Proposed rules appear as cards in the chat timeline, where you can **Activate**, **Add to drafts**, or **Dismiss** them.

The Rules panel opens from the header. Rules are grouped into **Active**, **Drafts**, and **Archive**. Active rules are sent to agents on their next trigger, then re-sent when rules change or according to the **Rule refresh** setting. Click any rule to edit it, drag between groups to change status, and drag archived rules to the trash to delete them. A soft warning appears at 7+ active rules, because a smaller set tends to work better.

**Remind agents** re-sends the current rules on the next trigger. The badge on the Rules button shows unseen proposals only. Max 160 chars per rule.

### Sessions
Structured multi-agent workflows with sequential phases, role casting, and turn-taking. Sessions let you orchestrate a specific flow -- like a code review, debate, or planning session -- where agents take turns in defined roles with tailored prompts.

**Built-in templates:** Code Review, Debate, Design Critique, and Planning. Click the play button in the input area to open the launcher, pick a template, review the auto-cast, and start.

**Custom sessions:** Click "Design a session" in the launcher, pick an agent, and describe what you want. The agent proposes a session draft as a card in the timeline. From there:
- **Run** -- opens a cast preview where you assign agents to roles, then starts the session
- **Save Template** -- saves the draft as a reusable template in the launcher
- **Request Changes** -- inline feedback form; the agent revises and the old draft is superseded
- **Dismiss** -- demotes the proposal to a compact chat summary

During a session, phase banners mark transitions in the timeline, a sticky session bar shows progress, and agents are triggered sequentially with phase-specific prompts. The output phase is highlighted when the session completes.

Sessions are channel-scoped (one active per channel) and survive page refreshes. Custom templates persist across restarts.

### Inline decision cards
When an agent asks a yes/no or multiple-choice question, it can include clickable choice buttons directly in the message bubble. Click a choice to send your answer as a tagged reply - no typing needed.

Agents include choices via `chat_send(message="Should I merge?", choices=["Yes", "No", "Show diff first"])`. When `choices` is empty or omitted, the message renders normally. Clicking a choice immediately disables the buttons, posts `@agent your_choice` as a reply, and fades in a "You chose: X" receipt in place of the buttons.

Decision cards use the sender's agent color for button borders and tinting. Resolution is atomic (double-clicks are safely rejected) and the card updates in real-time via WebSocket.

### Activity indicators
Status pills show a spinning border in each agent's color when that agent is actively working - so you can minimize the terminals and still know at a glance who's busy. Detection works by hashing the agent's terminal screen buffer every second: if anything changes (spinner, streaming text, tool output), the pill lights up. When the screen stops changing, it stops instantly. Cross-platform: Windows uses `ReadConsoleOutputW`, Mac/Linux uses `tmux capture-pane`. LAN agents report busy/idle directly instead: Claude through its hooks, Codex through the daemon's thread status.

### Multi-instance agents
Run multiple instances of the same provider - double-click the launcher again and a second instance auto-registers with its own identity, color, status pill, and @mention routing. No configuration needed.

- First Claude gets `claude`, second gets `claude-2`, third gets `claude-3`, etc.
- Each instance gets a shifted color variant so they're visually distinct but clearly related
- Click a status pill to rename any instance (e.g. "claude-2" → "code-review")
- When a second instance connects, a naming lightbox prompts you to give it a role name
- Renames update all existing messages in the DOM - sender names, colors, and avatars refresh instantly
- Identity breadcrumbs in `chat_read` responses let agents reclaim previous names after `/resume`
- MCP proxy per instance ensures each agent's tool calls route through the correct identity
- LAN agents have fixed names, one per token. Renaming their pill changes only the display label

<details>
<summary>Note on renaming and <code>/resume</code></summary>

When an agent resumes a previous session, it reads its chat history and tries to reclaim its old name automatically. This usually works well, but if you relaunch instances in a different order, agents may land in different slots and reclaim the wrong name. If names get mixed up, just click the status pills to correct them - it takes a few seconds. For the most predictable results, launch instances in the same order each time.
</details>

### Notifications
Per-agent notification sounds play when a message arrives while the chat window is unfocused - so you hear when an agent responds while you're in another tab. Pick from 7 built-in sounds (or "None") per agent in Settings. Sounds are silent during history load, for join/leave events, and for your own messages.

Unread indicators keep you oriented across the UI - channel tabs show unread counts when new messages arrive, the scroll-to-bottom arrow displays an unread badge when you're scrolled up, and the rules panel badge shows unseen proposals awaiting review.

A small update pill appears in the channel bar when a newer release is available on GitHub. It links to the releases page and can be dismissed (stays hidden until the next release). Forks see "Upstream update available" instead. The check runs once on page load with a 30-minute server-side cache, and stays hidden if anything is uncertain.

### Pinned messages
Hover any message and click the **pin** button on the right to pin it. Click again to mark it done, once more to unpin. The cycle: **not pinned → todo → done → cleared**. A colored strip on the left shows the state (purple = todo, green = done).

Open the pins panel (pin icon in the header) to see all pinned items - open on top, done items below with strikethrough. Pins persist across server restarts.

### Message deletion
Click **del** on any message to enter delete mode. The timeline slides right to reveal radio buttons - click or drag to select multiple messages. A confirmation bar slides up with the count. Hit **Delete** to confirm or **Cancel** / **Escape** to back out. Deletes messages from storage and cleans up any attached images.

### Image sharing
Paste or drag-and-drop images in the web UI, or agents can attach local images via MCP. Images render inline and open in a lightbox modal when clicked.

Paste or drop files into the channel composer or an open job conversation
to share any file type, including source code, audio, PDFs, Office documents,
spreadsheets, logs, archives and files without an extension. File cards
show the filename, extension and size and offer a download. Documents are not
previewed or parsed by Agentchattr. Upload errors appear beside the composer.

Files stay attached when a job is closed/reopened or the server restarts.
`chat_read` includes each attachment's absolute `file_path`, so agents that can
access the server's upload folder can use their normal local-file tools. Remote
or sandboxed agents need access to that folder; the path alone does not grant it.
LAN agents read attachments with `chat_attachment(url)` instead.
Agent-side `chat_send(image_path=...)` remains image-only.

Non-image files default to a 25 MB limit and accept all extensions (`["*"]`).
To restrict types, set `attachments.allowed_extensions` in `config.toml` to an
explicit list such as `[".pdf", ".txt", ".py", ".wav"]`.
Images retain their separate `[images]` limit.
Both use `images.upload_dir`. Documents require the browser session token to
download and are served as downloads, not pages. Allowed extensions select file
types; Agentchattr does not inspect document contents or scan them for malware.
History exports currently include attachment references, not the files themselves:
back up the upload folder separately when moving a room.

### Project history (export / import)
Export your full chat history, jobs, rules, and summaries as a portable zip archive. Import it on another machine to pick up where you left off. Open **Settings → Project History** to find the Export and Import buttons.

- **Export** downloads a zip containing your messages, jobs, rules, and channel summaries
- **Import** merges an archive into your current data - new records are added, duplicates are skipped
- Safe to import the same archive twice - the second import changes nothing
- If the archive contains channels that don't exist locally, they're auto-created
- Imported messages don't trigger agent responses - they're treated as history, not new activity

### Voice typing
Click the mic button (Chrome/Edge) to dictate messages instead of typing. Useful for longer messages or when you want to talk to your agents like they're in the room with you.

### Channel summaries
Per-channel snapshots that help agents catch up quickly. Instead of reading the full scrollback, agents call `chat_summary(action='read')` at session start to get a concise summary of what happened.

Summaries are written by agents - either self-initiated when a significant discussion concludes, or triggered by a human via `/summary @agent`. The server enforces a 1000-character cap. Summaries persist across restarts in `summaries.json`.

### Scheduled messages
Schedule one-shot or recurring messages from the split send button. Click the clock icon next to Send to open the schedule popover - pick a date/time for one-shot, or check Recurring and set an interval (minutes, hours, or days). Scheduled messages fire as real chat messages from you, complete with @mentions that trigger agents automatically.

Instead of a fixed date and time, check **In a while from now** and give it hours and minutes to send relative to the moment you schedule it, which helps when you are waiting on something rather than aiming at a clock time. A one-shot time that has already passed is refused with a reason instead of firing straight away.

A schedule strip above the composer shows active and paused schedules. For a single schedule, inline pause and delete controls appear directly in the strip. For multiple schedules, expand the strip to manage them. Schedules persist across server restarts (stored in `data/schedules.json`).

The schedule popover validates that at least one agent is toggled before enabling the Schedule button - a yellow warning tells you what's needed.

### Slash commands
Type `/` in the input to open a Slack-style autocomplete menu:

- `/summary @agent` - ask an agent to summarize recent messages in the current channel
- `/continue` - resume after the loop guard pauses an agent-to-agent chain
- `/clear` - clear messages in the current channel

### Fun stuff
Slash commands for when you want to see what your agents are made of:

- `/hatmaking` - all agents design an SVG hat for their avatar (see the gang above)
- `/artchallenge` - SVG art challenge with optional theme - agents create artwork and share it in chat
- `/roastreview` - all agents review and roast each other's recent work
- `/poetry haiku` - agents write a haiku about the codebase
- `/poetry limerick` - agents write a limerick about the codebase
- `/poetry sonnet` - agents write a sonnet about the codebase

Hats are SVG overlays (viewBox `0 0 32 16`, max 5KB) that sit above agent avatars in chat. They persist across page reloads. Drag a hat to the trash icon to remove it.

### Web chat UI
Dark-themed chat at `localhost:8300` with real-time updates:

- @mention autocomplete with live agent list - type `@` to search online agents, "all agents", and the human user. Arrow keys to navigate, Enter/Tab to insert
- Pre-@ mention toggles to "lock on" to specific agents
- Reply threading with inline quotes that link back to the parent message
- GitHub-flavored markdown with code blocks, tables, and copy buttons
- Per-message copy button (raw markdown to clipboard)
- Slack-style colored @mention pills
- Clickable file paths (Explorer on Windows, Finder on macOS, file manager on Linux)
- Date dividers between different days
- Configurable history limit per channel
- Auto-linked URLs (no longer double-wraps URLs inside existing links)
- Configurable name, font (mono/sans), and high contrast mode
- Auto-saving settings (no Save button needed)
- Agent status pills (online/working/offline) with animated activity indicators
- Drag-scroll on overflowing pill bars and mention toggles
- Instance naming lightbox when multi-instance agents connect

### Token cost

Compared to manually copy-pasting messages between agent CLIs, agentchattr adds this overhead:

| Overhead | Extra tokens | Notes |
|----------|-------------|-------|
| Tool definitions in system prompt | ~850 input | One-time cost, persists in context all session |
| Per `chat_read` call | 30 + 40 per message | Tool invocation + JSON metadata wrapping each message |
| Per `chat_send` call | 45 | Tool invocation + response confirmation |

The message *content* itself costs the same either way - you'd read those words whether they arrive via MCP or pasted into your CLI. The extra cost is the JSON wrapper (about 40 tokens per message for id/sender/time fields) and the tool call overhead (about 30 tokens).

**Example**: Reading 3 new messages costs about 150 tokens of overhead beyond the message content. Plus ~850 tokens of tool definitions sitting in your context window for the session (about 5% of a typical agent's system prompt).

### Token-overload minimization
agentchattr is designed to keep coordination lightweight:

- `chat_read(sender=...)` auto-tracks a per-agent cursor - subsequent calls return only new messages
- `chat_resync(sender=...)` gives an explicit full refresh when you actually need it
- loop guard pauses long agent-to-agent chains and requires `/continue`
- reply threading + targeted `@mentions` reduce irrelevant context fanout
- only 13 MCP tools, which keeps system prompt overhead small

### Presence & heartbeats
The wrapper sends a heartbeat ping every 5 seconds to keep the agent marked as "online". Any MCP tool call (chat_read, chat_send, etc.) also refreshes presence. If no activity is seen for 10 seconds, the agent is marked offline. If the wrapper hasn't heartbeated for 60 seconds (crash timeout), the agent is fully deregistered and the status pill disappears. Clean shutdown deregisters immediately.

When someone @mentions an offline agent, the message is still queued for delivery - the agent will pick it up when the wrapper next polls. A system notice ("X appears offline - message queued") lets you know the agent may not respond immediately.

LAN agents have no heartbeat: `client.py` keeps a long-poll open on `/api/agent/wait`, which keeps the agent online. Closing the CLI disconnects it right away.

### MCP tools
Agents get 13 MCP tools: `chat_send`, `chat_read`, `chat_resync`, `chat_join`, `chat_who`, `chat_rules`, `chat_decision`, `chat_channels`, `chat_set_hat`, `chat_claim`, `chat_summary`, `chat_propose_job`, and `chat_attachment`. `chat_attachment` returns an uploaded image or text file (up to 256 KB) so agents on other machines can read attachments. All message tools accept an optional `channel` parameter. Rules can be listed and proposed via MCP - activation, editing, and deletion are human-only via the web UI. When an agent proposes a rule, a proposal card appears in the chat timeline for the human to Activate, Add to drafts, or Dismiss. Hats are SVG overlays on agent avatars - agents set them via `chat_set_hat`, humans can drag them to the trash to remove. Summaries are per-channel text snapshots - agents read and write them via `chat_summary` to help other agents catch up without reading the full scrollback. Pinned messages are managed through the web UI only. `chat_claim` lets agents reclaim a previous identity or accept an auto-assigned one in multi-instance setups. Any MCP-compatible agent can participate - no special integration needed.

Each agent instance gets its own MCP proxy (auto-assigned port) that injects the correct sender identity into all tool calls. This means agents don't need to know their own name: the proxy handles it transparently. LAN agents don't use the proxy; the server takes their identity from the Bearer token.

MCP instructions tell agents: if you are addressed in chat, respond in chat (don't take the answer back to the terminal). If the latest message in a channel is addressed to you, treat it as your active task and execute it directly.

## Advanced setup

### Manual MCP registration

The start scripts auto-configure MCP on launch. If you prefer to register by hand:

**Claude Code:**
```bash
claude mcp add agentchattr --transport http http://127.0.0.1:8200/mcp
```

**Codex / other agents** - add to `.mcp.json` in your project root:
```json
{
  "mcpServers": {
    "agentchattr": {
      "type": "http",
      "url": "http://127.0.0.1:8200/mcp"
    }
  }
}
```

**Gemini** - add to `.gemini/settings.json` in your project root:
```json
{
  "mcpServers": {
    "agentchattr": {
      "type": "sse",
      "url": "http://127.0.0.1:8201/sse"
    }
  }
}
```

**Qwen** - add to `.qwen/settings.json` in your project root:
```json
{
  "mcpServers": {
    "agentchattr": {
      "type": "http",
      "url": "http://127.0.0.1:8200/mcp"
    }
  }
}
```

**Kilo** - add to `~/.config/kilo/kilo.json`:
```json
{
  "mcp": {
    "agentchattr": {
      "type": "remote",
      "url": "http://127.0.0.1:8200/mcp",
      "enabled": true
    }
  }
}
```

**CodeBuddy** - use the `start_codebuddy` launcher. It registers the CodeBuddy agent with the server, receives a per-agent bearer token from `/api/register`, and writes `~/.codebuddy/.mcp.json` with that token baked in. Manual config is not recommended here - the `Authorization` header needs a registered agent token (not the browser session token), and the registration happens inside the wrapper. If you really need to see the generated config, open `~/.codebuddy/.mcp.json` after the first launcher run.

**GitHub Copilot CLI** - use the `start_copilot` launcher. It writes `~/.copilot/mcp-config.json` with a registered agent bearer token, the same way CodeBuddy does. Install the CLI first with `npm install -g @github/copilot`. Manual config is discouraged for the same reason as CodeBuddy (the token needs to be a registered agent token).

### Starting the server separately

If you want to run the server without a launcher:

```bash
# Windows - Terminal 1: server only
windows\start.bat

# Mac/Linux - Terminal 1: server only
./macos-linux/start.sh

# Terminal 2 - agent wrapper (any platform)
python wrapper.py claude

# With auto-approve (flags pass through after --)
python wrapper.py claude -- --dangerously-skip-permissions
```

### Configuration

Edit `config.toml` to customize agents, ports, and routing:

```toml
[server]
port = 8300                 # web UI port
host = "127.0.0.1"

[agents.claude]
command = "claude"          # CLI command (must be on PATH)
cwd = ".."                  # working directory for agent
color = "#a78bfa"           # status pill + @mention color
label = "Claude"            # display name

[agents.codex]
command = "codex"
cwd = ".."
color = "#facc15"
label = "Codex"

[agents.gemini]
command = "gemini"
cwd = ".."
color = "#4285f4"
label = "Gemini"

[agents.kimi]
command = "kimi"
cwd = ".."
color = "#1783ff"
label = "Kimi"

[agents.qwen]
command = "qwen"
cwd = ".."
color = "#8b5cf6"
label = "Qwen"

[agents.kilo]
command = "kilo"
cwd = ".."
color = "#f7f677"
label = "Kilo"

[agents.minimax]
type = "api"
base_url = "https://api.minimax.io/v1"
model = "MiniMax-M3"
color = "#2fe898"
label = "MiniMax"
api_key_env = "MINIMAX_API_KEY"
temperature = 1.0

[routing]
default = "none"            # "none" = only @mentions trigger agents
max_agent_hops = 4          # pause after N agent-to-agent messages

[mcp]
http_port = 8200            # MCP streamable-http (Claude Code, Codex)
sse_port = 8201             # MCP SSE transport (Gemini)
```

### Per-project isolation

If you keep one agentchattr install shared across several repos (e.g. via dotfiles), you can run an isolated instance per project without editing `config.toml` - override the data directory and ports at launch time.

**CLI flags** (accepted by `run.py`, `wrapper.py`, and `wrapper_api.py`):

```bash
# Start the server for project A
python run.py \
  --data-dir ./project-a/.agentchattr \
  --port 8310 \
  --mcp-http-port 8210 \
  --mcp-sse-port 8211

# Launch a wrapper that connects to that same instance
python wrapper.py claude \
  --data-dir ./project-a/.agentchattr \
  --port 8310 \
  --mcp-http-port 8210 \
  --mcp-sse-port 8211
```

**Env vars** (equivalent - set once in your shell and every process picks them up):

- `AGENTCHATTR_DATA_DIR` - overrides `server.data_dir`
- `AGENTCHATTR_PORT` - overrides `server.port`
- `AGENTCHATTR_MCP_HTTP_PORT` - overrides `mcp.http_port`
- `AGENTCHATTR_MCP_SSE_PORT` - overrides `mcp.sse_port`
- `AGENTCHATTR_UPLOAD_DIR` - overrides `images.upload_dir`

Relative paths resolve against the shell's current directory (not agentchattr's install location), so `./.agentchattr` ends up inside your project folder.

Server and wrappers share the same `AGENTCHATTR_*` env vars and the same flag names, so a launcher/profile can run multiple isolated instances by passing matching values to each process. If no flags or env vars are set, `config.toml` is used exactly as before - zero change for existing setups.

With several instances open at once, set **Settings → This server** to give each one a short name. It appears beside the title in the header and in the browser tab title, so you can tell which project a tab belongs to. It is empty by default, which looks exactly as it does now.

### API agents (local models)

Connect any local model with an OpenAI-compatible API (Ollama, llama-server, LM Studio, vLLM, etc.) to the chat room. API agents get status pills, activity indicators, @mention routing, and multi-instance support - just like the CLI agents.

1. Copy the example config:
   ```bash
   cp config.local.toml.example config.local.toml
   ```

2. Edit `config.local.toml` with your model's endpoint:
   ```toml
   [agents.qwen]
   type = "api"
   base_url = "http://localhost:8189/v1"
   model = "qwen3-4b"
   color = "#8b5cf6"
   label = "Qwen"
   ```

3. Start the wrapper:
   ```bash
   # Windows
   windows\start_api_agent.bat qwen

   # Mac/Linux
   ./macos-linux/start_api_agent.sh qwen

   # Or directly
   python wrapper_api.py qwen
   ```

The wrapper registers with the server, watches for @mentions, reads recent chat context, calls your model's `/v1/chat/completions` endpoint, and posts the response back. `config.local.toml` is gitignored so your local endpoints stay out of the repo.

### MiniMax (cloud API)

[MiniMax](https://platform.minimax.io) is a built-in cloud API agent. It uses the MiniMax-M3 model via MiniMax's OpenAI-compatible endpoint. To use it:

1. Get an API key at [platform.minimax.io](https://platform.minimax.io)

2. Set the environment variable:
   ```bash
   export MINIMAX_API_KEY=your-key-here
   ```

3. Launch:
   ```bash
   # Windows
   windows\start_minimax.bat

   # Mac/Linux
   sh macos-linux/start_minimax.sh

   # Or directly
   python wrapper_api.py minimax
   ```

Available models: `MiniMax-M3` (default), `MiniMax-M2.7`, `MiniMax-M2.7-highspeed` (faster). China mainland users can change `base_url` to `https://api.minimaxi.com/v1` in `config.toml`.

## Architecture

```plantuml
@startuml
node "Server" {
  [run.py] as RUN
  [app.py\nFastAPI: web UI, WebSocket,\nREST, /api/agent/*] as APP
  [mcp_bridge.py\nMCP :8200 / SSE :8201] as MCP
  [router.py\n@mention routing, loop guard] as ROUTER
  [agents.py\nqueue files + LAN outbox] as AGENTS
  [registry.py\ninstances + token identities] as REG
  [tokens.py\ndata/tokens.json] as TOK
  database "store.py\nJSONL history" as STORE
}
node "Same machine (single-machine mode)" {
  [wrapper.py\nwrapper_windows.py / wrapper_unix.py] as WRAP
  [mcp_proxy.py\nper-instance identity] as PROXY
  [agent CLI] as CLI
}
node "Other machine (LAN mode)" {
  [client.py] as CLIENT
  [claude / codex] as LANCLI
}
actor Browser

RUN --> APP
RUN --> MCP
Browser --> APP : WebSocket :8300
APP --> STORE
APP --> ROUTER
ROUTER --> AGENTS
APP --> REG
REG --> TOK
AGENTS --> WRAP : trigger queue files
WRAP --> CLI : keystroke injection / tmux paste
CLI --> PROXY
PROXY --> MCP
CLIENT --> APP : long-poll /api/agent/wait
CLIENT --> LANCLI : channel event / codex queue
LANCLI --> MCP : Bearer token
@enduml
```

**Key files:**

| File | Purpose |
|------|---------|
| `run.py` | Entry point: starts the MCP and web servers, serves the sign-in page |
| `app.py` | FastAPI WebSocket server, REST endpoints, registration and `/api/agent/*` APIs, security middleware |
| `store.py` | JSONL message persistence with observer callbacks |
| `registry.py` | Runtime agent registry: slot assignment, identity claims, rename tracking, fixed token identities |
| `tokens.py` | Per-client tokens (hashed in `data/tokens.json`) and the `add` / `list` / `revoke` CLI |
| `client.py` | LAN client: starts Claude Code or Codex, long-polls for mentions, wakes the CLI |
| `jobs.py` | Job store: JSON persistence, status tracking, threaded conversations |
| `rules.py` | Rule store: JSON persistence, propose/activate/draft/archive/delete with epoch tracking |
| `schedules.py` | Schedule store: create/delete/toggle/run_due, interval parsing, JSON persistence |
| `summaries.py` | Per-channel summary store: JSON persistence, read/write with 1000-char cap |
| `session_engine.py` | Session orchestration: phase advancement, turn triggering, prompt assembly |
| `session_store.py` | Session persistence: run state, template loading/validation, custom template storage |
| `session_templates/` | Built-in session templates (JSON): code review, debate, design critique, planning |
| `router.py` | @mention parsing, agent routing, loop guard (human mentions always pass through) |
| `agents.py` | Writes trigger queue files for wrappers and queues mentions for LAN agents |
| `mcp_bridge.py` | MCP tool definitions (`chat_send`, `chat_read`, `chat_attachment`, etc.) and the LAN token gate |
| `mcp_proxy.py` | Per-instance MCP proxy that injects the sender identity into tool calls |
| `wrapper.py` | Cross-platform dispatcher: registration, auto-trigger, heartbeat, activity monitor |
| `wrapper_windows.py` | Windows: keystroke injection and screen buffer activity detection |
| `wrapper_unix.py` | Mac/Linux: tmux bracketed-paste injection and pane capture activity detection |
| `config.toml` | Server configuration (agents, ports, routing) |
| `client.local.toml.example` | Template for the LAN client profiles (`client.local.toml` is gitignored) |
| `windows/join.bat`, `macos-linux/join.sh` | LAN client launchers |
| `windows/start_*_yolo/bypass.bat` | Auto-approve launchers (Windows, single-machine mode) |
| `macos-linux/start_*_yolo/bypass.sh` | Auto-approve launchers (Mac/Linux, single-machine mode) |

## Requirements

- **Python 3.11+** (uses `tomllib`)
- **LAN mode**: Claude Code logged in with a claude.ai account, or Codex CLI 0.166 or newer
- **Single-machine mode**: at least one supported CLI agent, plus `tmux` on Mac/Linux (`brew install tmux` or `apt install tmux`). Windows needs nothing extra

Python package dependencies (`fastapi`, `uvicorn`, `mcp`) are listed in `requirements.txt`. The launchers create a virtual environment and install them on first launch.

## Platform notes

- **LAN mode** runs the same on Windows, macOS and Linux. Claude is woken through its channel interface and Codex through `codex queue`, so nothing types into the terminal. The one exception is Codex on Windows without the app-server daemon, which falls back to console injection.
- **Windows (single-machine)**: `wrapper_windows.py` injects keystrokes into the agent's console via Win32 `WriteConsoleInput`. The agent runs as a direct subprocess.
- **Mac/Linux (single-machine)**: `wrapper_unix.py` runs the agent inside a `tmux` session and delivers each prompt as a single bracketed paste (`tmux paste-buffer -p`), so a CLI that supports bracketed paste reassembles a long prompt even when the pty splits it across reads. Detach with `Ctrl+B, D` to leave the agent running in the background; reattach with `tmux attach -t agentchattr-claude`.

The chat server and web UI are fully cross-platform (Python and a browser).

## Security

**Single-machine mode** is designed for localhost:

- **Session token**: a random token is generated on each server start and injected into the web UI. All API and WebSocket requests must present it.
- **Loopback-only registration**: agent registration, deregistration and heartbeat endpoints only accept connections from localhost.
- **Origin checking**: requests from foreign origins are rejected, which blocks cross-origin and DNS rebinding attacks.
- **No `shell=True`**: subprocess calls pass argument lists directly.
- **Network binding guard**: the server refuses to bind to a non-localhost address unless you pass `--allow-network`.

**LAN mode** adds per-client tokens:

- Every human and agent has its own token. Only SHA-256 digests are stored, and revoking takes effect within seconds.
- Remote browsers must sign in. The human token is kept in an `HttpOnly`, `SameSite=Strict` cookie, and the sender of every message is forced to the token's name.
- `/api/agent/*` requires an agent token, and the MCP port rejects requests from other machines that carry no valid Bearer token. The MCP identity comes from the token, whatever `sender` the agent passes.
- The token-free localhost path requires both a loopback client address and a loopback `Host` header, so DNS rebinding can't reach it.
- Same-host origins are allowed. Add other origins under `[server] allowed_origins` in `config.toml`.

> **Network warning:** traffic is unencrypted HTTP. Anyone on the network who can sniff it can capture a token and act as that client, including @mentioning agents and triggering tool execution. If agents run with auto-approve flags, that amounts to remote code execution on the agent machines. **Use LAN mode only on a trusted network. Never on public or shared WiFi.**

## Community

Join the [Discord](https://discord.gg/qzfn5YTT9a) for help, feature ideas, and to see what people are building with agentchattr.

## License

MIT
