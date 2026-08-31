-- PostgreSQL workers claim one job atomically with FOR UPDATE SKIP LOCKED.
ALTER TABLE deployments
    ADD COLUMN claimed_by text,
    ADD COLUMN claim_expires_at timestamptz,
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    ADD COLUMN fencing_token bigint;

CREATE INDEX deployments_claimable
    ON deployments (created_at, id)
    WHERE state = 'queued';
