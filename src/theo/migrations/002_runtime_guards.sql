ALTER TABLE actions ADD COLUMN critic_status TEXT NOT NULL DEFAULT 'unchecked' CHECK(critic_status IN ('unchecked','passed','blocked'));
CREATE TABLE worker_processes(run_id TEXT PRIMARY KEY REFERENCES runs(id),owner_id TEXT NOT NULL REFERENCES owners(id),pid INTEGER NOT NULL,birth_time REAL NOT NULL,recorded_at REAL NOT NULL);
CREATE TABLE qualification_results(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),kind TEXT NOT NULL,backend TEXT,fingerprint TEXT,status TEXT NOT NULL,evidence TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE tool_receipts(owner_id TEXT NOT NULL REFERENCES owners(id),job_id TEXT NOT NULL REFERENCES jobs(id),semantic_key TEXT NOT NULL,result TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(owner_id,job_id,semantic_key));
