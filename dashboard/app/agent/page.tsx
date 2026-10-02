"use client";

import { useEffect, useState } from "react";

import { AgentConfigForm } from "@/components/AgentConfigForm";
import { AppShell } from "@/components/AppShell";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import type { AgentConfig } from "@/lib/types";

export default function AgentPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getAgentConfig(), []);
  const [draft, setDraft] = useState<AgentConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  async function save() {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const updated = await api.updateAgentConfig(draft);
      setDraft(updated);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save agent config");
    } finally {
      setSaving(false);
    }
  }

  return (
    <AppShell
      title="Agent Configuration"
      subtitle="Safe settings UI — credentials are never shown here"
    >
      {saveError ? (
        <div className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {saveError}
        </div>
      ) : null}
      {saved ? (
        <div className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          Agent configuration saved.
        </div>
      ) : null}
      <DataView
        loading={loading}
        error={error}
        data={draft}
        emptyTitle="Agent config unavailable."
        onRetry={reload}
      >
        {(value) => (
          <div className="space-y-4">
            <AgentConfigForm value={value} readOnly={false} onChange={setDraft} />
            <button
              type="button"
              disabled={saving}
              onClick={() => void save()}
              className="rounded-md bg-moss px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save configuration"}
            </button>
          </div>
        )}
      </DataView>
    </AppShell>
  );
}
