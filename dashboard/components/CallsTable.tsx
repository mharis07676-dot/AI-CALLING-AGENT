"use client";

import Link from "next/link";

import { StatusBadge } from "@/components/StatusBadge";
import { formatDateTime, formatDuration, formatPhone } from "@/lib/format";
import type { Call } from "@/lib/types";

export function CallsTable({
  calls,
  emptyTitle = "No calls found.",
}: {
  calls: Call[];
  emptyTitle?: string;
}) {
  if (calls.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-moss/20 bg-white px-6 py-10 text-center text-sm text-moss/65">
        {emptyTitle}
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white shadow-sm">
      <table className="min-w-[900px] w-full text-left text-sm">
        <thead className="bg-moss text-sand">
          <tr>
            <th className="px-4 py-3 font-semibold">Customer</th>
            <th className="px-4 py-3 font-semibold">Phone</th>
            <th className="px-4 py-3 font-semibold">Direction</th>
            <th className="px-4 py-3 font-semibold">Status</th>
            <th className="px-4 py-3 font-semibold">Started At</th>
            <th className="px-4 py-3 font-semibold">Duration</th>
            <th className="px-4 py-3 font-semibold">Intent</th>
            <th className="px-4 py-3 font-semibold">Outcome</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call) => (
            <tr key={call.id} className="border-t border-moss/10 hover:bg-mist/70">
              <td className="px-4 py-3">
                <Link href={`/calls/${call.id}`} className="font-medium text-leaf hover:underline">
                  {call.customer_id ? `Customer ${call.customer_id.slice(0, 8)}` : "Unknown"}
                </Link>
              </td>
              <td className="px-4 py-3">{formatPhone(call.from_number)}</td>
              <td className="px-4 py-3 capitalize">{call.direction}</td>
              <td className="px-4 py-3">
                <StatusBadge status={call.status} />
              </td>
              <td className="px-4 py-3">{formatDateTime(call.started_at ?? call.created_at)}</td>
              <td className="px-4 py-3">{formatDuration(call.duration_seconds)}</td>
              <td className="px-4 py-3">{call.intent ?? "—"}</td>
              <td className="px-4 py-3">{call.failure_reason ?? call.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
