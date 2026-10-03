-- Call recording storage metadata (extends 003_call_recordings.sql)
-- No new required columns: Call.recording_* fields already cover status/format/key/size.
-- This migration documents production defaults and is safe to re-run.

-- Ensure recording status index exists for dashboard filters
CREATE INDEX IF NOT EXISTS ix_calls_recording_status ON calls (recording_status);
CREATE INDEX IF NOT EXISTS ix_calls_recording_storage_key ON calls (recording_storage_key);

COMMENT ON COLUMN calls.recording_status IS 'recording|processing|ready|failed';
COMMENT ON COLUMN calls.recording_storage_key IS 'Object key in local/B2/S3 storage — never a public URL';
COMMENT ON COLUMN calls.recording_format IS 'mp3 or wav final artifact format';
