-- Cancellation must survive a completed producer job and an in-flight send.
-- Keep remote uncertainty/receipts distinct from the decision to stop retries.
ALTER TABLE actions ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1));
