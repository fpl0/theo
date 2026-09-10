CREATE TABLE candidate_rounds(
    change_id TEXT NOT NULL REFERENCES changes(id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    base_commit TEXT,
    coding_job_id TEXT UNIQUE,
    review_job_id TEXT UNIQUE,
    reason TEXT NOT NULL,
    PRIMARY KEY(change_id,revision)
);
INSERT INTO candidate_rounds
SELECT id,1,NULL,json_extract(request,'$.coding_job_id'),json_extract(request,'$.review_job_id'),'initial'
FROM changes;
