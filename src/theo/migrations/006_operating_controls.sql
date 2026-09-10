-- Capacity lane is separate from the authority that originated work.
ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'requested'
    CHECK(origin IN ('requested','autonomous','system'));
UPDATE jobs SET origin='autonomous' WHERE semantic_key LIKE 'autonomy:%';
UPDATE jobs SET origin='system' WHERE kind='critic';
WITH RECURSIVE inherited(id, origin) AS (
    SELECT id,origin FROM jobs WHERE parent_id IS NULL
    UNION ALL SELECT j.id,p.origin FROM jobs j JOIN inherited p ON j.parent_id=p.id
)
UPDATE jobs SET origin=(SELECT origin FROM inherited WHERE inherited.id=jobs.id)
WHERE id IN (SELECT id FROM inherited);

-- Preserve existing pauses when upgrading; fresh owners are seeded separately.
INSERT INTO control(owner_id,key,value)
    SELECT id,'autonomy_paused',COALESCE((SELECT value FROM control WHERE owner_id=owners.id AND key='background_paused'),'true') FROM owners;
INSERT INTO control(owner_id,key,value)
    SELECT id,'requested_work_paused',COALESCE((SELECT value FROM control WHERE owner_id=owners.id AND key='background_paused'),'true') FROM owners;
INSERT INTO control(owner_id,key,value) SELECT id,'deployments_paused','false' FROM owners;
INSERT INTO control(owner_id,key,value) SELECT id,'runtime_control_revision','0' FROM owners;
CREATE TABLE runtime_control_events(
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(id),
    conversation_id TEXT REFERENCES conversations(id),
    job_id TEXT REFERENCES jobs(id),
    scope TEXT NOT NULL,
    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL
);
