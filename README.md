# Forge

**Describe an automation once. Forge builds it, verifies it, deploys it and keeps it running** — locally, on your own
RTX GPUs, with no cloud inference.

- Natural language → typed workflow IR → static checks → offline tests → dry-run → **signed deploy approval** → durable, scheduled runs.
- Deterministic by default: deployed workflows call models only at explicit `llm` / `decide` steps.
- **Laya** (non-autoregressive System 1) for fast, grounded decisions; a local LLM (System 2) when Laya isn't sure.
- Windows desktop (UI Automation), browser (Playwright), Excel-backed records, MCP tools, self-built pure-compute tools.
`excel.create` builds a new verified `.xlsx` from headers and rows without opening Excel, and `excel.open` launches
it in Microsoft Excel using Windows application registration when Office is not on PATH. Existing files are never
overwritten by `excel.create`.

Browser automation opens a visible window by default. A purple Forge cursor shows each target, with a highlight
and click pulse, while your Windows pointer stays free. Fields fill immediately rather than character by character.
In Settings, use `browser.headless` to hide the window, `browser.show_cursor` to toggle the overlay, and
`browser.action_delay_ms` to adjust the pause before visible actions (default 300 ms, range 0–2000).
Restart Forge after changing these settings. Existing workflow calls with an explicit `headless` value keep their
behavior; changing the launch mode of an already-open profile requires restarting Forge and never closes your
current form automatically. The cursor covers webpage content, not browser chrome or native dialogs.

Browser tools can open separate tabs with `browser.open` and `new_tab=true`; `browser.download` is for files.

- Default-deny permissions, approvals bound to exact actions, emergency stop, hash-chained audit log.
- A terminal UI that works over SSH; closing it never stops anything.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Decisions](docs/DECISIONS.md) · [Status, benchmarks, roadmap](docs/ROADMAP.md)

## Quick start (Windows, NVIDIA GPU)
```powershell
winget install Ollama.Ollama
ollama pull qwen3:8b; ollama pull qwen3:4b; ollama pull qwen3-embedding:0.6b; ollama pull qwen2.5vl:7b
git clone <this repo>; cd forge
uv sync                      # CUDA torch via the pytorch-cu126 index (see pyproject.toml)
uv run forge                 # first run: setup wizard → starts the background daemon → opens the TUI
```
That's it. `forge` always does the same thing: run setup if it was never completed, start `forged` in the background if
it isn't running (it keeps running when you close the TUI), and open the terminal UI. `forge -c` resumes your last chat.

| Command | What it does |
|---|---|
| `forge setup` | hardware check → model profile → Ollama + models → browser → Laya → start-at-login (idempotent; `--yes` for defaults) |
| `forge doctor` | health checks with a fix for every problem |
| `forge config list / get / set / edit` | settings (validated, comments preserved, audited; live where possible) |
| `forge restart` / `forge shutdown` | restart or stop the background daemon |
| `forge install-startup` | start forged at logon (needed for unattended runs; a Windows service runs in Session 0 and can't see the desktop) |

## Try it
```powershell
uv run forge chat "Write hello.txt in my workspace with 'hi', then read it back"
uv run forge compile "Every weekday at 9am fetch https://example.com and save its title to ${FORGE_HOME}/workspace/watch/<date>.md, then notify me"
uv run forge approvals ; uv run forge approve <id>
uv run forge workflow import plugins\careerpilot\job_watch.yaml   # CareerPilot: job discovery → Excel tracker
uv run forge bench desktop --runs 3                               # UIA vs Laya+escalation vs LLM-every-step
uv run pytest
```
CareerPilot: edit `~/.forge/careerpilot/profile.yaml` and mark true facts `verified: true` before asking for
cover letters. Forge never submits applications or sends messages without your approval, never scrapes sites that
forbid it, and never bypasses CAPTCHAs.

## TUI
Type `/` for commands (floating autocomplete, Tab completes) or Ctrl+P for the command palette.

| Key | |
|---|---|
| Ctrl+O | settings editor (every setting, live/restart badge, validation) |
| Ctrl+T | live work monitor (runs + current step, agent turns, desktop lease, model calls) |
| Ctrl+X | session switcher |
| `/model` · `/tools` · `/doctor` | model picker (pin per capability, pull models) · enable/disable tools · health checks |
| Ctrl+C | clear draft, then interrupt the agent · Ctrl+D quit on empty prompt |
| Ctrl+E twice | EMERGENCY STOP |
| F1–F7 | Chat · Runs · Workflows · Approvals · Records · Memory · Activity |

Approvals pop up as a modal showing exactly what will happen (a approve, d deny, Esc later). Messages typed while the
agent is busy are queued. There is deliberately no "skip approvals" mode, and the agent cannot change settings.

## Layout
```
src/forge/   agent/ browser/ daemon/ desktop/ memory/ models/ policy/ records/ runtime/ tools/ tui/ workflows/
plugins/careerpilot/   collections, tools, job_watch workflow (a plugin, not core)
tests/                 50 tests: policy, runtime + crash recovery, Excel, router, browser, MCP, toolgen, CareerPilot,
                       migrations, settings, doctor, detached daemon + headless TUI
```
