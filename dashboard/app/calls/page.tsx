"use client";

import { useMemo, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { CallsTable } from "@/components/CallsTable";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import type { Call } from "@/lib/types";

type Filter =
  | "all"
  | "inbound"
  | "outbound"
  | "completed"
  | "failed"
  | "transferred";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "inbound", label: "Inbound" },
  { id: "outbound", label: "Outbound" },
  { id: "completed", label: "Completed" },
  { id: "failed", label: "Failed" },
  { id: "transferred", label: "Human Handoff" },
];

const PAGE_SIZE = 12;

export default function CallsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getCalls(), []);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const filtered = useMemo(() => {
    const calls = data ?? [];
    return calls.filter((call) => {
      if (filter === "inbound" || filter === "outbound") {
        if (call.direction !== filter) return false;
      } else if (filter === "completed" || filter === "failed" || filter === "transferred") {
        if (call.status !== filter && !(filter === "failed" && call.status === "rejected")) return false;
      }

      const q = query.trim().toLowerCase();
      if (!q) return true;
      return (
        call.from_number.toLowerCase().includes(q) ||
        call.to_number.toLowerCase().includes(q) ||
        (call.customer_id ?? "").toLowerCase().includes(q) ||
        (call.intent ?? "").toLowerCase().includes(q)
      );
    });
  }, [data, filter, query]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <AppShell title="Call History" subtitle="Search and review past voice sessions">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-2">
          {FILTERS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => {
                setFilter(item.id);
                setPage(1);
              }}
              className={`rounded-full px-3 py-1.5 text-xs font-semibold ${
                filter === item.id ? "bg-moss text-white" : "bg-white text-moss ring-1 ring-moss/15"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setPage(1);
          }}
          placeholder="Search phone, customer, intent…"
          className="w-full rounded-md border border-moss/15 bg-white px-3 py-2 text-sm lg:max-w-sm"
        />
      </div>

      <DataView
        loading={loading}
        error={error}
        data={data as Call[] | null}
        emptyTitle="No calls found."
        onRetry={reload}
      >
        {() => (
          <div className="space-y-4">
            <CallsTable calls={pageItems} emptyTitle="No calls match this filter." />
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
    </AppShell>
  );
}
