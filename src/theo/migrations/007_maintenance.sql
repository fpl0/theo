CREATE TABLE maintenance_intents(
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    coding_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    review_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    request TEXT NOT NULL CHECK(json_valid(request)),
    change_id TEXT UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    submission TEXT CHECK(submission IS NULL OR json_valid(submission)),
    review TEXT CHECK(review IS NULL OR json_valid(review)),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    rollback_requested TEXT,
    projection TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(projection)),
    created_at REAL NOT NULL
);
CREATE TABLE maintenance_events(
    sequence INTEGER PRIMARY KEY,
    change_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL CHECK(json_valid(detail)),
    received_at REAL NOT NULL
);
