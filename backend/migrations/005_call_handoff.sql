-- Human handoff / live agent transfer fields on calls
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_requested BOOLEAN DEFAULT FALSE;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_requested_at TIMESTAMPTZ;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_status VARCHAR(32);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_reason TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_connected_at TIMESTAMPTZ;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_calls_handoff_status ON calls (handoff_status);
CREATE INDEX IF NOT EXISTS ix_calls_handoff_requested ON calls (handoff_requested);
