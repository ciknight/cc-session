<div align="center">

# Claude Sessions

**Mission control for your Claude Code sessions.**

Monitor every project, understand what Claude is doing, respond to approvals, and keep conversations moving—from one real-time web dashboard.

[Documentation](https://ciknight.github.io/cc-session/) · [English](README.md) · [简体中文](README.zh-CN.md)

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-6C4CF1)](https://docs.astral.sh/uv/)
[![MIT license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](#contributing)

</div>

## Why Claude Sessions?

Claude Code is great at running tasks. Once several sessions are active, keeping track of them becomes the hard part: Which one is still working? Which one needs approval? What did that agent finish? Where did that long conversation go?

**Claude Sessions puts those answers in one place.** It watches your existing Claude Code transcripts, turns them into a live multi-project dashboard, and lets you continue or host conversations directly from your browser.

- **See everything at a glance** — sessions grouped by project, with working, idle, and stale states.
- **Know what is happening** — inspect messages, tool calls, diffs, tasks, subagents, models, Git branches, and token usage.
- **Keep work moving** — send follow-ups, queue messages while Claude is busy, interrupt a turn, or start a new session.
- **Stay in control** — handle permission requests and `AskUserQuestion` prompts in the browser.
- **Use it anywhere on your trusted network** — a responsive interface works on desktop and mobile, with live updates over SSE.
- **Build on top of it** — consume the JSON API, SSE event stream, or optional webhooks in your own tools.

## Highlights

| Capability | What you get |
| --- | --- |
| **Live session dashboard** | Automatic discovery, incremental updates, project grouping, search, filters, unread replies, pinning, aliases, and archiving. |
| **Rich conversation view** | Markdown responses, user messages, tool results, diffs, task lists, and subagent activity in a readable chat interface. |
| **Persistent hosted sessions** | A long-running `stream-json` Claude process with streaming text, queued follow-ups, model selection, effort controls, and interruption. |
| **Flexible approvals** | Choose browser-based approval, Claude CLI permission handling, or automatic approval per hosted session. |
| **Fast on large histories** | Incremental JSONL parsing, persistent scan caching, lazy subagent loading, and paginated message windows. |
| **Self-contained frontend** | Local frontend assets with no external CDN required; the backend uses Python, Jinja2, and psutil. |
| **Browser notifications** | Alerts for new replies, pending approvals, and failures while the tab is hidden, with short-turn filtering and an unread count in the tab title. |
| **System panel** | Host CPU and memory, optional sleep/timed sleep/restart, keep-awake while sessions work (Windows), and orphaned-process cleanup. |
| **cc-switch integration** | Optional project-local profile switching when `cc-switch` is installed, with confirmation and backups. |

## Quick start

### Prerequisites

- Python **3.9+**
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://code.claude.com/) installed and configured if you want to send messages or host sessions

Monitoring existing transcripts does not start Claude or consume model tokens. Remote messaging and hosted sessions use your normal Claude Code configuration.

### Run it

```bash
git clone https://github.com/ciknight/cc-session.git
cd cc-session

# Recommended for local-only use
uv run monitor.py --host 127.0.0.1
```

Open **http://127.0.0.1:8765**. Existing Claude Code sessions are discovered automatically.

To make the dashboard available on a trusted private network, use the default host or set one explicitly:

```bash
uv run monitor.py --host 0.0.0.0 --port 8765
```

> [!WARNING]
> The server has **no authentication**. It can send messages to Claude, change settings, approve tool calls, and perform host power actions. Never expose it to the public internet or an untrusted network. See [Security](#security).

## Choose your workflow

| Mode | Best for | How it works |
| --- | --- | --- |
| **Monitor** | Watching sessions already running in terminals | Incrementally reads Claude Code transcript files without changing them. |
| **One-shot messaging** | Occasional browser follow-ups | Runs one `claude -p --resume` turn per message, with per-session queuing. |
| **Hosted session** | Long-running, interactive work | Keeps a persistent Claude process alive for streaming, queued messages, approvals, and live control. |

When hosting an existing session, exit its terminal process first so two processes do not write to the same conversation.

Hosted sessions default to the **`full`** approval mode, which automatically approves permission requests. Switch to **`ask`** in the session view when you want to review each request, or **`auto`** to use Claude Code's own permission mode.

## Configuration

Common examples:

```bash
# Use another port and mark sessions stale after 10 minutes
uv run monitor.py --port 9000 --stale 600

# Forward session events to another local service
uv run monitor.py --webhook http://localhost:3000/hook

# Pass arguments to Claude child processes
uv run monitor.py --claude-args "--model sonnet"
```

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | HTTP listen address. Use `127.0.0.1` for local-only access. |
| `--port` | `8765` | HTTP listen port. |
| `--interval` | `1.0` | Transcript polling interval in seconds. |
| `--refresh` | `3.0` | Dashboard fallback refresh interval in seconds. |
| `--stale` | `300` | Seconds without content events before a session is shown as stale. |
| `--webhook` | none | POST each emitted event to this URL. |
| `--claude-bin` | `claude` | Path to the Claude Code CLI. |
| `--claude-args` | empty | Extra arguments passed to Claude child processes. |
| `--msg-timeout` | `600` | Maximum execution time for a one-shot message, in seconds. |
| `--permission-timeout` | `300` | Seconds to wait for hosted approval before denying; `0` waits indefinitely. |
| `--scan-subagents` | off | Scan all subagent transcripts at startup instead of loading them on demand. |

To monitor a different Claude configuration directory:

```bash
CLAUDE_CONFIG_DIR=/path/to/.claude uv run monitor.py --host 127.0.0.1
```

## JSON API, SSE, and webhooks

The dashboard is only one client of the service. You can use the same live data in scripts, notification tools, or another UI.

| Endpoint | Purpose |
| --- | --- |
| `GET /sessions` | List session summaries; use `?full=1` for details or `?include_subagents=1` to load subagents. |
| `GET /sessions/<id>` | Get one session, including tasks, subagents, interactions, and hosted state. |
| `GET /sessions/<id>/messages` | Read paginated conversation messages (`limit`, `before`, `from`). |
| `GET /events` | Subscribe to the SSE stream; `?session=<id>` filters to one session. |
| `GET /interactions` | List pending permission and question interactions. |
| `POST /sessions/<id>/message` | Queue or send a message with `{"text":"..."}`. |
| `POST /new` | Start a new one-shot session in a project directory. |
| `POST /hosted` | Start or resume a persistent hosted session. |
| `GET /sessions/<id>/jobs` | Inspect running, queued, and recently completed jobs. |
| `POST /sessions/<id>/interrupt` | Interrupt the active hosted turn and clear its queued messages. |
| `POST /sessions/<id>/approval` | Set `ask`, `auto`, or `full` approval mode. |
| `POST /sessions/<id>/effort` | Set hosted effort to `low`, `medium`, `high`, or `default`. |
| `GET /jobs/<id>` | Query a messaging job. |
| `GET /healthz` | Health check. |
| `GET /system` | Host CPU, memory, sleep-timer, keep-awake, and orphaned-process status. |

For example:

```bash
curl http://127.0.0.1:8765/sessions
curl -N http://127.0.0.1:8765/events
```

## How it works

```text
~/.claude/projects/*/*.jsonl
              │
              ▼
   incremental polling + scan cache
              │
              ▼
         monitor.py ───────────► JSON API / SSE / webhooks
              │
              ▼
     responsive web dashboard
              │
              ├──► claude -p --resume       (one-shot)
              └──► persistent stream-json  (hosted)
```

Claude Sessions reads the transcript files Claude Code already writes. It maintains per-file offsets, handles partial lines, caches validated scan state across restarts, and parses conversation messages only when requested. Subagent transcripts are lazy-loaded by default.

The monitoring path does not rewrite your transcripts. Session organization and web preferences are stored separately under `<claude_dir>/claude-sessions/`.

## Development

Install dependencies and run the service:

```bash
uv sync
uv run monitor.py --host 127.0.0.1
```

Run the test suites:

```bash
# Python
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v

# Frontend controls
node --test tests/test_frontend_controls.js
```

On Windows PowerShell, set `PYTHONPATH` for the Python test command:

```powershell
$env:PYTHONPATH = '.'
uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

The project intentionally stays lightweight: a Python standard-library HTTP server, Jinja2 templates, psutil system sampling, and a self-contained JavaScript frontend.

The backend is organized as the `claude_sessions` package: `core` (scanning, jobs, events), `hosted` (persistent Claude processes), `session` and `transcript` (session state and message parsing), `http_api` (REST/SSE), plus `interactions`, `system`, `orphans`, `commands`, `util`, `config`, `hook`, and `app`. The root `monitor.py` remains the entry point and a compatibility facade that re-exports the public symbols.

The bilingual documentation website lives in [`docs/`](docs/). It is deployed to [GitHub Pages](https://ciknight.github.io/cc-session/) by [`.github/workflows/pages.yml`](.github/workflows/pages.yml) on pushes to `main` that change the site. To preview it locally, open `docs/index.html` or serve that directory with any static HTTP server.

## Contributing

Issues, bug reports, documentation improvements, and pull requests are welcome.

1. Open an issue first for substantial changes so the approach can be discussed.
2. Keep changes focused and preserve the lightweight, local-first design.
3. Run the relevant Python and frontend tests.
4. Update both the English and Chinese README when user-facing behavior changes.

## License

Claude Sessions is released under the [MIT License](LICENSE). You are free to use, modify, and distribute it, subject to the license terms.

## Security

- There is **no authentication or multi-user isolation**. Run it only on a trusted machine or private network.
- Prefer `--host 127.0.0.1`; the default `0.0.0.0` listens on all interfaces.
- Remote messaging can execute Claude with your local permissions and environment.
- Hosted **`full`** approval automatically allows permission requests. Use **`ask`** when human review is required.
- The system panel can sleep or restart the host. Browser confirmation is not a security boundary.
- Do not place the service behind a public reverse proxy unless you add appropriate authentication, authorization, transport security, and request protections yourself.

---

Claude Sessions is an independent community project and is not affiliated with or endorsed by Anthropic.

If it makes your Claude Code workflow easier, consider starring the repository and sharing it with others.
