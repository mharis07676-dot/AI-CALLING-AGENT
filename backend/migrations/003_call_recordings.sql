-- Call recording metadata (Twilio dual-channel SIP audio)
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_status VARCHAR(32);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_format VARCHAR(16);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_duration_seconds INTEGER;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_size_bytes INTEGER;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_storage_key VARCHAR(512);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_provider VARCHAR(32);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_provider_sid VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_calls_recording_status ON calls (recording_status);
CREATE INDEX IF NOT EXISTS ix_calls_recording_provider_sid ON calls (recording_provider_sid);
