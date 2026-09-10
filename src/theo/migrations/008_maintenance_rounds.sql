CREATE TABLE maintenance_rounds(
    intent_id TEXT NOT NULL REFERENCES maintenance_intents(id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    coding_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    review_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    submission TEXT CHECK(submission IS NULL OR json_valid(submission)),
    review TEXT CHECK(review IS NULL OR json_valid(review)),
    PRIMARY KEY(intent_id,revision)
);
INSERT INTO maintenance_rounds
SELECT id,revision,coding_job_id,review_job_id,submission,review FROM maintenance_intents;
