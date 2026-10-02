-- SQL migration for Campaign table (existing Postgres / Railway).
-- Prefer app bootstrap create_all for fresh environments.
--
-- Exact command (from repo root, with DATABASE_URL_SYNC set):
--   psql "$DATABASE_URL_SYNC" -f backend/migrations/001_campaigns.sql
--
-- Docker Compose:
--   docker compose exec -T db psql -U synas -d synas_agents < backend/migrations/001_campaigns.sql

DO $$ BEGIN
    CREATE TYPE campaignstatus AS ENUM ('draft', 'running', 'paused', 'completed');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name VARCHAR(255) NOT NULL,
    status campaignstatus NOT NULL DEFAULT 'draft',
    total_contacts INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    dialing INTEGER NOT NULL DEFAULT 0,
    in_progress INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    no_answer INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    interested INTEGER NOT NULL DEFAULT 0,
    opted_out INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_campaigns_tenant_id ON campaigns (tenant_id);
