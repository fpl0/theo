-- Operational correlation only. Does not participate in business identity or authorization.
CREATE TABLE telemetry_links(kind TEXT NOT NULL, entity_id TEXT NOT NULL, traceparent TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(kind,entity_id));
CREATE INDEX telemetry_links_created ON telemetry_links(created_at);
