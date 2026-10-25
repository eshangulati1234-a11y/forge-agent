# Forge — Status, Benchmarks, Roadmap

> Spec documents 1 (requirements, condensed), 9 (roadmap) and 10 (testing and benchmarks).

## 1. What exists after day 1 (prototype grade)

| Area | State | Evidence |
|---|---|---|
| Daemon, API, auth, event stream | ✓ working | `forge status`, TUI live events |
| Agent harness (validation, repeat guard, approvals, subagents, budgets, persistence) | ✓ working | live tool-call chat; network-approval round trip |
| Model router (capability, residency, queue, circuit breaker, fallback) | ✓ working | `tests/test_router.py` incl. live Ollama tool call |
| Laya System 1 + LLM System 2 | ✓ working | desktop benchmark below |
| Workflow IR, static checks, tests, dry-run, deploy gate | ✓ working | `tests/test_runtime.py`, live NL compile → deploy → run |
| Durable runtime (leases, checkpoints, retries, dedupe, approvals, crash recovery) | ✓ working | kill-mid-run test |
| Scheduler (cron, interval, file; webhook endpoint) | ✓ working (cron/interval tested; file trigger untested) | tests |
| Records + Excel (validation, transitions, export, three-way import, conflicts, backups) | ✓ working | `tests/test_excel.py`, live tracker |
| Desktop perception / executor / decision loop / session lease | ✓ working on Calculator + WinForms | benchmark |
| Browser (Playwright, allowlists, risky-click refusal, CAPTCHA hand-off) | ✓ working on a local site | `tests/test_browser.py` |
| MCP client bridge | ✓ working (stdio) | `tests/test_mcp.py` |
| Self-building tools (pure compute) | ✓ working, slow (~10 min on 8B with thinking) | live generation of `gen.parse_salary_range` |
| Self-repair (diagnose → patch → verify → approval → rollback) | ⚠ implemented, **not exercised on a real drift yet** | – |
| Memory (typed, provenance, embeddings) | ⚠ implemented, lightly tested | – |
| TUI | ✓ working (headless pilot) | – |
| CareerPilot `job_watch` | ✓ live: 49 real postings → 3 matched → scored → tracker.xlsx in 34 s | – |
| Startup at logon | implemented (`forge install-startup`), **not installed** (it's your choice to run it) | – |

**Test suite: 37 passing** (`uv run pytest`).

## 2. Measured benchmarks (RTX 3070 Laptop 8 GB, 2026-10-25)

### Desktop: identical tasks, 3 runs × 5 tasks per mode
Tasks: three Calculator computations, plus two WinForms application-form tasks that include distractor and high-risk buttons. Ground truth comes from a file the form writes.

| Mode | Success | Wrong-action rate | p50 decision | p50 observe | p50 action | p50 task | Total | Escalations | Peak GPU |
|---|---|---|---|---|---|---|---|---|---|
| Deterministic UIA replay | **15/15** | 0 | – | – | 142 ms | **1.1 s** | 16.8 s | – | 5.3 GB |
| Laya + LLM escalation | **15/15** | 0 | **101 ms** | 235 ms | 149 ms | **6.0 s** | 88 s | 10.5 % of decisions | 8.0 GB |
| LLM every step (qwen3:8b) | **15/15** | 0 | 1,021 ms | 240 ms | 158 ms | 10.6 s | 152 s | – | 8.0 GB |

Laya calibration on the logged decisions:
- n = 57; raw top-choice accuracy 94.7 %.
- The lowest threshold reaching 95 % precision is 0.339 (coverage 94.7 %).
- **The default stays at 0.6** until there's more data from harder, real apps.

**How we got here (all measured today; see ARCHITECTURE §5):**
- First Laya run: 40 % success. Two causes. The pre-ranker dropped the correct button (no digit↔word match), and the benchmark scored WinForms id-vs-name matches as wrong.
- After those fixes: 20 %. Putting the full control list in Laya's state collapsed accuracy (7/8 → 0/8 in isolation).
- Minimal state: 80 %.
- Deterministic value read-back for typed fields: 100 %.

**Caveat:** these are 5 simple tasks on 2 apps. They prove the pipeline works and show where latency goes; they don't establish general desktop competence.

### Other measurements
- Laya: 45 ms per decision warm, 39 s cold load, ~1.6 GB VRAM.
- qwen3:8b: 56 tok/s, 5.5 GB at 8k ctx (q8_0 KV). Together they use 7.9 of 8.2 GB.
- NL compile (first attempt correct on a 3-step cron workflow): ~1–2 min. Model-written test expectations were wrong 2 out of 2 times, so they're advisory.
- Tool generation: fails with thinking off (3/3 rounds); succeeds in round 3 with thinking on, in 582 s.

## 3. Five highest-risk assumptions to test next
1. **Laya on real, dense UIs** (Excel ribbon, Outlook, browser-rendered apps with 300+ controls). The ~20-option budget plus lexical pre-ranking may not hold up. Experiment: 30 tasks across Excel, Explorer, Settings and Outlook. Compare `predict_shortlist` with embeddings against the lexical pre-ranker, and refit the calibration.
2. **Unattended GUI work on a machine you're using.** The idle gate plus pattern-first actions may still collide with you. Experiment: a second Windows user over RDP loopback, or a Hyper-V VM as the GUI worker. Measure UIA reliability with RDP minimised or disconnected.
3. **8B compile quality on multi-branch workflows** (foreach + branch + approval). Experiment: 25 graded requests. Measure first-pass validity, rounds to valid, and semantic correctness judged by hand. Move compile to the 3060 with a 14B model if it falls under 70 %.
4. **Self-repair on real drift.** Experiment: rename buttons in the WinForms app and change a job-board JSON shape. Measure the share of correct proposed patches and false repairs, and confirm rollback triggers.
5. **Excel round trip with a human in the loop.** You edit tracker.xlsx while job_watch runs. Experiment: a scripted concurrent edit + export soak. Verify zero lost edits and that every conflict is recorded.

## 4. First 20 engineering tasks (dependency order)
1. Move forged to the RTX 3060 desktop; add the Blade as a `[[nodes]]` worker (Ollama bound to the VPN IP, firewall rule restricted to the desktop).
2. Remote desktop-worker protocol: a small `forge worker` agent on the Blade that exposes the desktop/browser tools over an authenticated channel to the daemon.
3. Router: measured per-model swap cost; VRAM-aware admission (refuse to load VL while 8B + Laya are resident on the same node).
4. Run the Laya hard-UI benchmark (risk 1) and switch large candidate sets to `predict_shortlist` with qwen3-embedding.
5. Record mode: watch a user perform a desktop task once (UIA focus/invoke events) and emit a deterministic workflow with selectors.
6. Selector self-heal telemetry: log every heal and promote repeated heals into a repair proposal.
7. Repair evaluation harness (risk 4) with injected drift cases.
8. Compiler graded benchmark (risk 3); split compile into plan → IR to cut tokens.
9. Workflow test DSL: typed assertions (`equals`, `contains`, `count >=`) instead of free Jinja, so 8B models write valid tests.
10. Email trigger + `email.*` tools via IMAP/SMTP or Microsoft Graph (credential via keyring; sending is `external`).
11. CareerPilot: email classification workflow → `recruitment_emails` + status transitions.
12. CareerPilot: truthful CV tailoring from verified facts (docx via python-docx) with a diff view in the TUI.
13. CareerPilot: portal form-fill via `browser.find` + `browser.act`; stop at `browser.submit` for approval; verify the confirmation page.
14. TUI conflict-resolution screen for `record_conflicts`; Excel import on file save (watchdog).
15. Windows Sandbox (or AppContainer) execution backend for generated tools with I/O; network-off by default.
16. Plugin signing: manifest hash pinned at install, approval to load.
17. Secrets UX: `forge secret set` (keyring), referenced by name in IR, never visible to models.
18. Soak test: 72 h with scheduled workflows, induced Ollama crashes and daemon restarts. Gate: zero lost or duplicated runs.
19. Metrics: per-workflow success rate, model-call counts per run (target: 0 for deterministic workflows), latency histograms in the TUI.
20. Packaging: `uv tool install forge`, `forge doctor` (checks Ollama, CUDA, keyring, Session, DPI), docs site.

## 5. Roadmap

| Phase | Weeks | Deliverables | Exit gate |
|---|---|---|---|
| **Prototype** (done) | day 1 | Everything in §1 | 37 tests green; live job_watch; 3-mode desktop benchmark |
| **Alpha** | 1–3 | Tasks 1–9: two-node topology, remote worker, record mode, hard-UI Laya benchmark, repair harness, typed test DSL | Laya ≥ 90 % step accuracy on 30 real-app tasks with calibrated escalation; compiler ≥ 70 % first-pass valid on 25 requests |
| **CareerPilot beta** | 4–6 | Tasks 10–14: email, CV/letters from verified facts, portal fill to the approval gate, conflict UI | 2 weeks of daily use with zero unapproved external actions and zero lost tracker edits |
| **Platform beta** | 7–10 | Tasks 15–19: sandboxed generated tools, plugin signing, secrets, soak, metrics | 72 h soak: 0 lost/duplicate runs; repair ≥ 50 % correct patches, 0 permission escalations |
| **1.0** | 11–14 | Task 20, docs, threat-model review, installer | External user installs from scratch in < 30 min; security review closed |

Arbitrary desktop automation and secure self-repair will **not** be "solved". 1.0 means measured success rates on a published task suite, with the failure modes documented.

## 6. How Forge differs from Hermes Agent-style agents
- **Compile once, run deterministically.** Hermes-style agents re-reason every time. A deployed Forge workflow runs tool steps with *zero* model calls, and model use is explicit (`llm` / `decide` steps) and visible in the graph.
- **System 1 / System 2 split.** Fast non-generative Laya decisions (≈100 ms) over grounded candidates, escalating to the LLM over the same enum, so neither system can choose something that doesn't exist. Measured as 1.8× faster end to end than LLM-every-step at equal success on the benchmark.
- **Durable by construction.** Leased runs, per-step checkpoints, idempotency and in-doubt detection for external actions survive a kill -9 (tested).
- **Security model rather than prompt hygiene.** Default-deny grants frozen at deploy, signed single-use approvals bound to exact arguments, a hash-chained audit, and repair that can't widen permissions.
- **Records you can trust.** A typed, auditable store with Excel as a verified, conflict-aware view, instead of the agent editing spreadsheets directly.
- **Local and free.** Everything ran on one 8 GB laptop GPU today, with no cloud inference.
