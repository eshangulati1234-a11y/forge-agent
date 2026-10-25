# Technology Decision Record

> Spec document 3. "Proven here" means exercised on this machine today (RTX 3070 Laptop, Windows 11).

| Subsystem | Selected | Alternatives considered | Why | Limitations / risks | Licence | Proven here |
|---|---|---|---|---|---|---|
| Language/runtime | Python 3.12 + asyncio, uv | TS/Node, Rust | pywinauto, openpyxl, Playwright, Laya, Textual and pywin32 are all first-class in Python | GIL; CPU-bound work goes to threads/subprocesses | PSF | ✓ |
| Inference server | **Ollama 0.40** (native API) | llama.cpp server, LM Studio, vLLM | One-command Windows install; native tool calls, JSON-schema `format`, `think` toggle, `keep_alive`, q8_0 KV cache | One request at a time on 8 GB; model swaps cost 5–10 s | MIT | ✓ |
| Remote inference / PAIR | OpenAI-compatible `kind=openai` node | NVIDIA PAIR now; LiteLLM | PAIR is beta (2026-09) and routes per request without pooling VRAM. Forge's router already does capability routing, so PAIR can sit behind a node entry later | PAIR not evaluated today | – | ✗ (deferred, by design) |
| Router | Own ~250-LOC `ModelRouter` | LiteLLM, PAIR | Capability, residency and queue-aware ranking, circuit breaker, fallback. No heavy dependency | Swap cost is estimated, not measured per model | – | ✓ (fallback test + live) |
| Reasoning/tool LLM | `qwen3:8b` Q4_K_M | Qwen2.5-7B, Llama 3.1 8B, Gemma | Best tool-calling and JSON reliability that fits next to Laya in 8 GB. Thinking mode on demand | Weak at writing correct tests and expectations without thinking mode | Apache-2.0 | ✓ |
| Vision | `qwen2.5vl:7b` | MiniCPM-V, Florence-2 | Good screenshot/UI reading at 7B | Can't be co-resident with the 8B on this card | Apache-2.0 | pulled, not benchmarked |
| Embeddings | `qwen3-embedding:0.6b` | nomic-embed-text, bge-m3 | Small, multilingual, served by the same Ollama | – | Apache-2.0 | pulled |
| System 1 decisions | **Laya 0.4.15** (`convaiinnovations/laya`, ModernBERT-large) | LLM for every action; rule-based matching | ~45 ms decisions; typed choice/score/yes-no; Windows/CUDA; Apache-2.0 | Over-confident raw scores; very sensitive to state framing (see ARCHITECTURE §5); ~20-option budget | Apache-2.0 | ✓ benchmarked |
| Desktop automation | **pywinauto (UIA backend)** + pywin32 + mss | Microsoft UFO, raw `uiautomation`, Power Automate | Pattern-based actions don't disturb the user; selectors are replayable | Slow trees in big apps; UWP quirks; no Session 0 | BSD | ✓ |
| Desktop agent design | Own observe-decide-act-verify loop | UFO (reference only) | UFO puts an LLM on each step; Forge compiles to selectors and uses Laya only for exploration and repair | – | – | ✓ |
| Browser | **Playwright** | Browser Use, Skyvern (references), Selenium | Robust, auto-waiting, network interception for allowlists, persistent profiles | Sites that forbid automation stay out of scope; no CAPTCHA bypass | Apache-2.0 | ✓ (local site test) |
| Workflow engine | **Own durable runtime on SQLite** | Temporal, Prefect, DBOS, Airflow | Single node: leases + checkpoints + idempotency in ~450 LOC with no extra server. Temporal needs a cluster plus Java/Go services | No horizontal workers yet (one daemon) | – | ✓ (kill test) |
| Scheduler | Own (croniter + watchdog) | APScheduler | Persisted next-fire, dedupe keys, misfire grace | – | MIT/BSD | ✓ |
| State | **SQLite WAL** | PostgreSQL | Zero-ops, transactional, fast enough for a personal agent | Single writer; move to Postgres when multi-host | Public domain | ✓ |
| Excel | **openpyxl** (generation/import) + **Excel COM** via pywin32 (edits that must preserve charts) | pandas, xlwings, Graph API | openpyxl gives full formatting/validation/charts without Excel installed; COM keeps rich workbooks intact | openpyxl drops charts and images on re-save of arbitrary files → COM path | MIT / PSF | ✓ (openpyxl); COM path untested |
| Records ↔ Excel sync | Export snapshot hashes + three-way import | Live COM sync; Excel as DB | Excel can't be the source of truth for concurrent writers | Conflicts need a human (TUI) | – | ✓ |
| Memory | SQLite + numpy cosine | sqlite-vec, Chroma, Qdrant | No extra service; fine below ~100k items | Brute force | – | partial |
| TUI | **Textual 8** | Rich-only, prompt_toolkit, Ratatui | Async, widgets, works over SSH, testable headless | Some terminals mangle Alt+Enter | MIT | ✓ (headless pilot) |
| API | FastAPI + WebSocket on loopback | gRPC | Simple, typed, streaming events | – | MIT | ✓ |
| Tool protocol | Own typed registry + **MCP client bridge** | MCP-only | Built-ins need side-effect metadata MCP lacks; MCP servers are bridged and approval-gated | MCP server sandboxing is up to the server | MIT | ✓ (stdio test) |
| Generated tools | Pure-compute, AST allowlist, isolated subprocess + Job Object | Windows Sandbox, Hyper-V VM, Docker | Works today with no admin rights | Not a security boundary; I/O tools must be composed, not generated | – | ✓ (live generation) |
| Secrets | Windows Credential Manager (keyring) | .env files, Vault | OS-protected, per user | Same-user processes can read it | MIT | ✓ |
| Notifications | Event bus → TUI + Windows toast | ntfy, email | Local, no account | No phone push yet | – | ✓ |

## Challenges to the original brief
1. **"Laya selects every desktop action" is the wrong default.** Compiled workflows should replay deterministic UIA selectors (1.1 s per task, no model, 100% on the benchmark). Laya earns its place in *exploration* (10× faster decisions than the LLM) and in *self-healing* drifted selectors. It should not sit in the hot path of a known procedure.
2. **Laya's raw confidence can't gate safety.** Measured: confidence 1.0 on a wrong choice when the correct option was missing. Forge relies on a complete grounded candidate set, deterministic verification and the policy engine; the confidence gate only decides when to escalate.
3. **Excel can't be the system of record** for an autonomous agent. SQLite is, and Excel is a verified view with a three-way merge.
4. **Self-generated tools with unrestricted I/O aren't safe on Windows without VM isolation.** Day 1 restricts generation to pure compute; anything with I/O is a composed workflow under normal grants.
5. **PAIR isn't needed for routing.** It may help later as an OpenAI-compatible fan-out across the two RTX boxes, but it doesn't pool VRAM.
6. **The 8 GB laptop is not a good primary server.** It fits 8B + Laya with 300 MB spare. Running forged on the 3060 desktop (12 GB) leaves room for the VL model or a 14B model at Q4, and keeps the laptop free as the GUI worker.
