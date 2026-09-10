-- A reminder is finished message text; work must run with fresh canonical context.
-- Existing reminders keep their exact meaning and delivery guarantees.
ALTER TABLE schedules ADD COLUMN mode TEXT NOT NULL DEFAULT 'reminder' CHECK(mode IN ('reminder','work'));
ALTER TABLE schedules ADD COLUMN origin TEXT NOT NULL DEFAULT 'requested' CHECK(origin IN ('requested','autonomous','system'));
