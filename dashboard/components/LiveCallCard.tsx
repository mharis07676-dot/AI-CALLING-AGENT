"use client";

import Link from "next/link";

import { StatusBadge } from "@/components/StatusBadge";
import { formatDuration, formatPhone } from "@/lib/format";
import type { Call } from "@/lib/types";

export function LiveCallCard({
  call,
  onHandoff,
  onHangup,
  handoffBusy,
  hangupBusy,
}: {
  call: Call;
  onHandoff?: (call: Call) => void;
  onHangup?: (call: Call) => void;
  handoffBusy?: boolean;
  hangupBusy?: boolean;
}) {
  return (
    <div className="rounded-xl border border-moss/10 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-ink">
            {formatPhone(call.customer_phone || call.from_number)}
          </p>
          <p className="mt-1 text-xs text-moss/60">
            {call.customer_name ||
              (call.customer_id ? `Customer ${call.customer_id.slice(0, 8)}` : "Unknown customer")}
          </p>
        </div>
        <StatusBadge status={call.status} />
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-moss/55">Direction</dt>
          <dd className="capitalize text-ink">{call.direction}</dd>
        </div>
        <div>
          <dt className="text-moss/55">Duration</dt>
          <dd className="text-ink">{formatDuration(call.duration_seconds)}</dd>
        </div>
        <div>
          <dt className="text-moss/55">Intent</dt>
          <dd className="text-ink">{call.intent ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-moss/55">Language</dt>
          <dd className="text-ink">{call.language ?? "—"}</dd>
        </div>
      </dl>

      <div className="mt-5 flex flex-wrap gap-2">
        <Link
          href={`/calls/${call.id}`}
          className="rounded-md bg-moss px-3 py-2 text-xs font-semibold text-white hover:bg-leaf"
        >
          View Call
        </Link>
        <button
          type="button"
          onClick={() => onHandoff?.(call)}
          disabled={!onHandoff || handoffBusy}
          className="rounded-md border border-moss/20 px-3 py-2 text-xs font-semibold text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          Human Handoff
        </button>
        <button
          type="button"
          onClick={() => onHangup?.(call)}
          disabled={!onHangup || hangupBusy}
          className="rounded-md border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          End Call
        </button>
      </div>
    </div>
  );
}
