"use client";

import { useMemo, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function AppointmentsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getAppointments(), []);
  const [status, setStatus] = useState("all");

  const filtered = useMemo(() => {
    const items = data ?? [];
    if (status === "all") return items;
    return items.filter((item) => item.status === status);
  }, [data, status]);

  return (
    <AppShell title="Appointments" subtitle="Visit bookings confirmed by the backend">
      <div className="mb-4">
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-moss/15 bg-white px-3 py-2 text-sm"
        >
          <option value="all">All statuses</option>
          <option value="pending">Requested</option>
          <option value="confirmed">Confirmed</option>
          <option value="cancelled">Cancelled</option>
          <option value="completed">Completed</option>
        </select>
      </div>

      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No appointments found."
        onRetry={reload}
      >
        {() => (
          <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white shadow-sm">
            <table className="min-w-[900px] w-full text-left text-sm">
              <thead className="bg-moss text-sand">
                <tr>
                  <th className="px-4 py-3">Customer</th>
                  <th className="px-4 py-3">Phone</th>
                  <th className="px-4 py-3">Property</th>
                  <th className="px-4 py-3">Date / Time</th>
                  <th className="px-4 py-3">Booking Code</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Source</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr key={item.id} className="border-t border-moss/10">
                    <td className="px-4 py-3">Customer {item.customer_id.slice(0, 8)}</td>
                    <td className="px-4 py-3">—</td>
                    <td className="px-4 py-3">{item.property_id ? item.property_id.slice(0, 8) : "—"}</td>
                    <td className="px-4 py-3">{formatDateTime(item.scheduled_at)}</td>
                    <td className="px-4 py-3 font-medium">{item.booking_code}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="px-4 py-3">{item.call_id ? "Voice call" : "Manual"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {filtered.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-moss/60">No appointments for this filter.</p>
            ) : null}
          </div>
        )}
      </DataView>
    </AppShell>
  );
}
