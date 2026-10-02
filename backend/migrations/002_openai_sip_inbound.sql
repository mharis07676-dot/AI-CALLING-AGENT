-- OpenAI Realtime SIP inbound support
ALTER TABLE calls ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ;

ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS provider_tool_call_id VARCHAR(128);
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_tool_calls_provider_tool_call_id ON tool_calls (provider_tool_call_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id UUID PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id),
    call_id UUID REFERENCES calls(id),
    scope VARCHAR(64) NOT NULL,
    key VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_scope_key ON idempotency_keys (scope, key);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_tenant_id ON idempotency_keys (tenant_id);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_call_id ON idempotency_keys (call_id);
