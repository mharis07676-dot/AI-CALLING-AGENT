"use client";

import Link from "next/link";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function HandoffsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getOpenHandoffs(), []);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function accept(id: string) {
    setBusyId(id);
    setActionError(null);
    try {
      await api.updateHandoff(id, { status: "accepted" });
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Accept failed");
    } finally {
      setBusyId(null);
    }
  }

  async function resolve(id: string) {
    setBusyId(id);
    setActionError(null);
    try {
      await api.updateHandoff(id, {
        status: "completed",
        resolution_notes: "Resolved from dashboard",
      });
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Resolve failed");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <AppShell title="Human Handoffs" subtitle="Open escalations waiting for staff">
      {actionError ? (
        <div className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {actionError}
        </div>
      ) : null}
      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No open handoffs."
        onRetry={reload}
      >
        {(handoffs) => (
          <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white shadow-sm">
            <table className="min-w-[980px] w-full text-left text-sm">
              <thead className="bg-moss text-sand">
                <tr>
                  <th className="px-4 py-3">Customer</th>
                  <th className="px-4 py-3">Phone</th>
                  <th className="px-4 py-3">Reason</th>
                  <th className="px-4 py-3">Call ID</th>
                  <th className="px-4 py-3">Requirements</th>
                  <th className="px-4 py-3">Requested At</th>
                  <th className="px-4 py-3">Assigned To</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {handoffs.map((item) => (
                  <tr key={item.id} className="border-t border-moss/10">
                    <td className="px-4 py-3">
                      {item.customer_name ||
                        (item.customer_id ? `Customer ${item.customer_id.slice(0, 8)}` : "—")}
                    </td>
                    <td className="px-4 py-3">{item.customer_phone ?? "—"}</td>
                    <td className="px-4 py-3">{item.reason}</td>
                    <td className="px-4 py-3">
                      <Link href={`/calls/${item.call_id}`} className="text-leaf hover:underline">
                        {item.call_id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      {Object.keys(item.context || {}).length
                        ? JSON.stringify(item.context)
                        : "—"}
                    </td>
                    <td className="px-4 py-3">{formatDateTime(item.created_at)}</td>
                    <td className="px-4 py-3">{item.assigned_user_id ?? "Unassigned"}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-2">
                        <Link
                          href={`/calls/${item.call_id}`}
                          className="rounded-md bg-moss px-2.5 py-1.5 text-xs font-semibold text-white"
                        >
                          View Call
                        </Link>
                        <button
                          type="button"
                          disabled={busyId === item.id || item.status === "accepted"}
                          onClick={() => void accept(item.id)}
                          className="rounded-md border border-moss/20 px-2.5 py-1.5 text-xs font-semibold text-ink disabled:opacity-45"
                        >
                          Accept
                        </button>
                        <button
                          type="button"
                          disabled={busyId === item.id}
                          onClick={() => void resolve(item.id)}
                          className="rounded-md border border-moss/20 px-2.5 py-1.5 text-xs font-semibold text-ink disabled:opacity-45"
                        >
                          Mark Resolved
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataView>
    </AppShell>
  );
}
