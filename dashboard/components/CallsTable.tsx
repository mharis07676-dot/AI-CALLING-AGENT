"use client";

import Link from "next/link";

import { CallRecordingPlayer } from "@/components/CallRecordingPlayer";
import { StatusBadge } from "@/components/StatusBadge";
import { formatDateTime, formatDuration, formatPhone } from "@/lib/format";
import type { Call } from "@/lib/types";

function recordingCell(call: Call) {
  const rec = call.recording;
  if (!rec?.status) {
    return <span className="text-moss/45">—</span>;
  }
  if (rec.status === "ready" && (rec.available || rec.playback_endpoint)) {
    return <CallRecordingPlayer callId={call.id} recording={rec} compact />;
  }
  if (rec.status === "processing" || rec.status === "recording" || rec.status === "pending") {
    return <span className="text-moss/70">Processing…</span>;
  }
  if (rec.status === "failed") {
    return <span className="text-red-700">Recording unavailable</span>;
  }
  return <span className="text-moss/55">{rec.status}</span>;
}

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
      <table className="min-w-[1100px] w-full text-left text-sm">
        <thead className="bg-moss text-sand">
          <tr>
            <th className="px-4 py-3 font-semibold">Customer</th>
            <th className="px-4 py-3 font-semibold">Phone</th>
            <th className="px-4 py-3 font-semibold">Direction</th>
            <th className="px-4 py-3 font-semibold">Status</th>
            <th className="px-4 py-3 font-semibold">Started At</th>
            <th className="px-4 py-3 font-semibold">Duration</th>
            <th className="px-4 py-3 font-semibold">Language</th>
            <th className="px-4 py-3 font-semibold">Recording</th>
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
              <td className="px-4 py-3">{call.language ?? "—"}</td>
              <td className="px-4 py-3">{recordingCell(call)}</td>
              <td className="px-4 py-3">
                {call.status === "completed" ? "Completed" : (call.failure_reason ?? call.status)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
