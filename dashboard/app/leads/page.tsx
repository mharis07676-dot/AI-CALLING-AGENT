"use client";

import { useMemo, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { LeadDetails } from "@/components/LeadDetails";
import { StatusBadge } from "@/components/StatusBadge";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { Lead } from "@/lib/types";

const PAGE_SIZE = 12;

export default function LeadsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getLeads(), []);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Lead | null>(null);

  const filtered = useMemo(() => {
    const leads = data ?? [];
    return leads.filter((lead) => {
      if (status !== "all" && lead.status !== status) return false;
      const q = query.trim().toLowerCase();
      if (!q) return true;
      return (
        (lead.location ?? "").toLowerCase().includes(q) ||
        (lead.property_type ?? "").toLowerCase().includes(q) ||
        (lead.customer_id ?? "").toLowerCase().includes(q) ||
        lead.purpose.toLowerCase().includes(q)
      );
    });
  }, [data, query, status]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <AppShell title="Leads" subtitle="Requirements captured by the voice agent">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setPage(1);
          }}
          className="rounded-md border border-moss/15 bg-white px-3 py-2 text-sm"
        >
          <option value="all">All statuses</option>
          <option value="new">New</option>
          <option value="qualified">Qualified</option>
          <option value="contacted">Contacted</option>
          <option value="booked">Visit Scheduled</option>
          <option value="closed">Closed</option>
          <option value="lost">Lost</option>
        </select>
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setPage(1);
          }}
          placeholder="Search location, type, purpose…"
          className="w-full rounded-md border border-moss/15 bg-white px-3 py-2 text-sm lg:max-w-sm"
        />
      </div>

      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No leads found."
        onRetry={reload}
      >
        {() => (
          <div className="space-y-4">
            <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white shadow-sm">
              <table className="min-w-[980px] w-full text-left text-sm">
                <thead className="bg-moss text-sand">
                  <tr>
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Phone</th>
                    <th className="px-4 py-3">Purpose</th>
                    <th className="px-4 py-3">Location</th>
                    <th className="px-4 py-3">Property Type</th>
                    <th className="px-4 py-3">Size</th>
                    <th className="px-4 py-3">Budget</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Created At</th>
                  </tr>
                </thead>
                <tbody>
                  {pageItems.map((lead) => (
                    <tr
                      key={lead.id}
                      className="cursor-pointer border-t border-moss/10 hover:bg-mist/70"
                      onClick={() => setSelected(lead)}
                    >
                      <td className="px-4 py-3">
                        {lead.customer_id ? `Customer ${lead.customer_id.slice(0, 8)}` : "—"}
                      </td>
                      <td className="px-4 py-3">—</td>
                      <td className="px-4 py-3 capitalize">{lead.purpose}</td>
                      <td className="px-4 py-3">{lead.location ?? "—"}</td>
                      <td className="px-4 py-3">{lead.property_type ?? "—"}</td>
                      <td className="px-4 py-3">{lead.size ?? "—"}</td>
                      <td className="px-4 py-3">
                        {lead.budget_max ?? lead.budget_min ?? "—"}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={lead.status} />
                      </td>
                      <td className="px-4 py-3">{formatDateTime(lead.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex items-center justify-between text-sm">
              <p className="text-moss/60">
                Showing {pageItems.length} of {filtered.length}
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
                  className="rounded-md border border-moss/15 px-3 py-1.5 disabled:opacity-40"
                >
                  Previous
                </button>
                <span className="px-2 py-1.5">
                  {page} / {totalPages}
                </span>
                <button
                  type="button"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                  className="rounded-md border border-moss/15 px-3 py-1.5 disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          </div>
        )}
      </DataView>

      {selected ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label="Close"
            onClick={() => setSelected(null)}
          />
          <div className="relative w-full max-w-2xl rounded-xl bg-white p-6 shadow-xl">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="font-display text-xl font-semibold">Lead details</h3>
              <button type="button" onClick={() => setSelected(null)} className="text-sm font-semibold">
                Close
              </button>
            </div>
            <LeadDetails lead={selected} />
            <p className="mt-4 text-xs text-moss/55">
              Phone/name are not included on LeadOut yet. Backend customer join endpoint is missing.
            </p>
          </div>
        </div>
      ) : null}
    </AppShell>
  );
}
