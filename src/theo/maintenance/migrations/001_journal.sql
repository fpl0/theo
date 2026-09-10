CREATE TABLE changes(
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    request TEXT NOT NULL CHECK(json_valid(request)),
    policy_hash TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_until REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    blocker TEXT,
    result TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result)),
    deadline REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX changes_ready ON changes(status,created_at);
CREATE TABLE events(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL REFERENCES changes(id),
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL CHECK(json_valid(detail)),
    created_at REAL NOT NULL,
    UNIQUE(change_id,revision)
);
CREATE TABLE candidates(
    change_id TEXT NOT NULL REFERENCES changes(id),
    revision INTEGER NOT NULL,
    identity TEXT NOT NULL CHECK(json_valid(identity)),
    accepted_at REAL NOT NULL,
    PRIMARY KEY(change_id,revision)
);
