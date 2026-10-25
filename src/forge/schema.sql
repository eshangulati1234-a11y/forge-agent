-- Forge state. SQLite (WAL) is the single source of truth; Excel is an import/export view.
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,                -- slug, e.g. careerpilot.job_watch
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    version INTEGER NOT NULL,
    ir_json TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    status TEXT NOT NULL,               -- draft | approved | deployed | retired | rejected
    created_by TEXT NOT NULL,           -- user | compiler | repair
    parent_version_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,                -- workflow_id:trigger_index
    workflow_id TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    next_fire_at REAL,
    last_fired_at REAL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions(id),
    workflow_id TEXT NOT NULL,
    trigger TEXT NOT NULL,              -- manual | schedule | file | webhook | dry_run | repair_test
    status TEXT NOT NULL,               -- queued | running | waiting_approval | waiting_user | succeeded | failed | cancelled
    priority INTEGER NOT NULL DEFAULT 5,
    inputs_json TEXT NOT NULL DEFAULT '{}',
    dry_run INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT UNIQUE,
    outputs_json TEXT,
    error TEXT,
    error_class TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, priority, created_at);

CREATE TABLE IF NOT EXISTS step_runs (
    run_id TEXT NOT NULL REFERENCES runs(id),
    step_path TEXT NOT NULL,            -- e.g. fetch | score[3].decide
    kind TEXT NOT NULL,
    status TEXT NOT NULL,               -- running | succeeded | failed | skipped | waiting_approval
    attempt INTEGER NOT NULL DEFAULT 1,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    error_class TEXT,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, step_path)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle', -- idle | running | interrupted
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,                 -- system | user | assistant | tool
    content TEXT NOT NULL DEFAULT '',
    tool_calls_json TEXT,
    tool_name TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    run_id TEXT,
    step_path TEXT,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    status TEXT NOT NULL,               -- ok | error | denied | needs_approval
    result_json TEXT,
    error TEXT,
    error_class TEXT,
    duration_ms INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- deploy | action | permission
    title TEXT NOT NULL,
    subject_json TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending | approved | denied | expired
    requested_by TEXT NOT NULL,
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT,
    reason TEXT,
    signature TEXT,
    used_at REAL                        -- action approvals are single-use
);
CREATE INDEX IF NOT EXISTS approvals_hash ON approvals(action_hash, status);

CREATE TABLE IF NOT EXISTS grants (
    workflow_id TEXT PRIMARY KEY,
    permissions_json TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    granted_at REAL NOT NULL
);

-- Hash-chained audit log: each row commits to the previous row's hash.
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- preference | fact | episode | skill | failure
    provenance TEXT NOT NULL,           -- user | verified | tool_observed | generated
    content TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    embedding BLOB,
    created_at REAL NOT NULL,
    last_used_at REAL,
    invalidated_at REAL
);

-- Generic structured records (collections are declared by plugins).
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection, id)
);

CREATE TABLE IF NOT EXISTS record_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    record_id TEXT NOT NULL,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,                -- workflow run id | excel_import | user
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS excel_exports (
    path TEXT NOT NULL,
    collection TEXT NOT NULL,
    exported_at REAL NOT NULL,
    row_hashes_json TEXT NOT NULL,      -- {record_id: hash at export time}
    db_versions_json TEXT NOT NULL,     -- {record_id: db version at export time}
    PRIMARY KEY (path, collection)
);

CREATE TABLE IF NOT EXISTS record_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    record_id TEXT NOT NULL,
    excel_json TEXT NOT NULL,
    db_json TEXT NOT NULL,
    detected_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT
);
