"use client";

import { useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { LiveCallCard } from "@/components/LiveCallCard";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import type { Call } from "@/lib/types";

export default function LiveCallsPage() {
  const { data, error, loading, reload, setData } = useAsyncData(() => api.getLiveCalls(), []);
  const [selected, setSelected] = useState<Call | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void api
        .getLiveCalls()
        .then(setData)
        .catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [setData]);

  async function requestHandoff() {
    if (!selected) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.createHandoff({
        call_id: selected.id,
        customer_id: selected.customer_id ?? undefined,
        reason: "Requested from live calls dashboard",
      });
      setSelected(null);
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Handoff request failed");
    } finally {
      setBusy(false);
    }
  }

  async function hangupCall(call: Call) {
    setBusy(true);
    setActionError(null);
    try {
      const result = await api.hangupCall(call.id);
      if (!result.success) {
        throw new Error(result.message || result.error || "Hangup failed");
      }
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Hangup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AppShell title="Live Calls" subtitle="Active sessions refresh every 4 seconds">
      {actionError ? (
        <div className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {actionError}
        </div>
      ) : null}

      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No live calls."
        emptyDescription="When customers call in, active sessions will appear here."
        onRetry={reload}
      >
            {(calls) => (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {calls.map((call) => (
              <LiveCallCard
                key={call.id}
                call={call}
                onHandoff={setSelected}
                onHangup={(item) => void hangupCall(item)}
                handoffBusy={busy}
                hangupBusy={busy}
              />
            ))}
          </div>
        )}
      </DataView>

      <ConfirmDialog
        open={Boolean(selected)}
        title="Request human handoff?"
        description="This will call POST /calls/handoffs for the selected live call."
        confirmLabel="Request Handoff"
        busy={busy}
        onCancel={() => setSelected(null)}
        onConfirm={() => void requestHandoff()}
      />
    </AppShell>
  );
}
