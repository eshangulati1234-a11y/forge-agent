# Forge

**Describe an automation once. Forge builds it, verifies it, deploys it and keeps it running** — locally, on your own
RTX GPUs, with no cloud inference.

- Natural language → typed workflow IR → static checks → offline tests → dry-run → **signed deploy approval** → durable, scheduled runs.
- Deterministic by default: deployed workflows call models only at explicit `llm` / `decide` steps.
- **Laya** (non-autoregressive System 1) for fast, grounded decisions; a local LLM (System 2) when Laya isn't sure.
- Windows desktop (UI Automation), browser (Playwright), Excel-backed records, MCP tools, self-built pure-compute tools.
- Default-deny permissions, approvals bound to exact actions, emergency stop, hash-chained audit log.
- A terminal UI that works over SSH; closing it never stops anything.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Decisions](docs/DECISIONS.md) · [Status, benchmarks, roadmap](docs/ROADMAP.md)

## Quick start (Windows, NVIDIA GPU)
```powershell
winget install Ollama.Ollama
ollama pull qwen3:8b; ollama pull qwen3:4b; ollama pull qwen3-embedding:0.6b; ollama pull qwen2.5vl:7b
git clone <this repo>; cd forge
uv sync                      # CUDA torch via the pytorch-cu126 index (see pyproject.toml)
uv run playwright install chromium
uv run forge init            # creates ~/.forge (forge.toml, token)
uv run forge daemon          # starts ollama serve if needed, then forged on 127.0.0.1:8765
uv run forge tui             # in another terminal, or over SSH
```
Optional: `uv run forge install-startup` registers forged as a logon task (needed for unattended desktop automation;
a Windows service runs in Session 0 and can't see the desktop).

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

## TUI keys
F1 Chat · F2 Runs · F3 Workflows · F4 Approvals (a approve / x deny) · F5 Records (e export) · F6 Memory · F7 Activity ·
Ctrl+X twice = EMERGENCY STOP · `/help` for slash commands.

## Layout
```
src/forge/   agent/ browser/ daemon/ desktop/ memory/ models/ policy/ records/ runtime/ tools/ tui/ workflows/
plugins/careerpilot/   collections, tools, job_watch workflow (a plugin, not core)
tests/                 37 tests: policy, runtime + crash recovery, Excel, router, browser, MCP, toolgen, CareerPilot
```
