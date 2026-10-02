"use client";

import Link from "next/link";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AppShell } from "@/components/AppShell";
import { CallsTable } from "@/components/CallsTable";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { StatCard } from "@/components/StatCard";
import { StatusBadge } from "@/components/StatusBadge";
import { api } from "@/lib/api";
import { computeDashboardStats, formatPhone } from "@/lib/format";

const COLORS = ["#3d7a5f", "#c45c26", "#1f3d32", "#64748b", "#0ea5e9", "#e11d48"];

export default function DashboardPage() {
  const { data, error, loading, reload } = useAsyncData(async () => {
    const [calls, live, leads, handoffs, appointments] = await Promise.all([
      api.getCalls(),
      api.getLiveCalls(),
      api.getLeads(),
      api.getOpenHandoffs(),
      api.getAppointments(),
    ]);
    const stats = computeDashboardStats(calls, leads, handoffs.length, appointments.length);
    const statusDistribution = Object.entries(
      calls.reduce<Record<string, number>>((acc, call) => {
        acc[call.status] = (acc[call.status] ?? 0) + 1;
        return acc;
      }, {}),
    ).map(([name, value]) => ({ name, value }));

    const callsPerDay = Object.entries(
      calls.reduce<Record<string, number>>((acc, call) => {
        const key = new Date(call.created_at).toISOString().slice(0, 10);
        acc[key] = (acc[key] ?? 0) + 1;
        return acc;
      }, {}),
    )
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-7)
      .map(([date, count]) => ({ date, count }));

    return {
      stats,
      live,
      recent: calls.slice(0, 8),
      statusDistribution,
      callsPerDay,
      leadsToday: stats.leadsToday,
      appointments: appointments.length,
    };
  }, []);

  return (
    <AppShell title="Dashboard" subtitle="Operational overview for Synas Labs voice agents">
      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No operational data yet."
        emptyDescription="Calls, leads, and appointments will appear here once activity starts."
        onRetry={reload}
      >
        {(view) => (
          <div className="space-y-8">
            <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard label="Active Calls" value={view.stats.activeCalls} />
              <StatCard label="Queued Calls" value={view.stats.queuedCalls} />
              <StatCard label="Calls Today" value={view.stats.callsToday} />
              <StatCard label="Completed" value={view.stats.completedCalls} />
              <StatCard label="Failed" value={view.stats.failedCalls} />
              <StatCard label="Leads Today" value={view.stats.leadsToday} />
              <StatCard label="Human Handoffs" value={view.stats.handoffs} />
              <StatCard label="Appointments" value={view.stats.appointments} />
            </section>

            <section className="grid gap-6 xl:grid-cols-2">
              <div>
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="font-display text-xl font-semibold">Live Calls</h3>
                  <Link href="/live-calls" className="text-sm font-semibold text-leaf hover:underline">
                    View all
                  </Link>
                </div>
                {view.live.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-moss/20 bg-white px-5 py-8 text-sm text-moss/65">
                    No active calls right now.
                  </div>
                ) : (
                  <div className="space-y-3">
                    {view.live.slice(0, 4).map((call) => (
                      <div
                        key={call.id}
                        className="flex items-center justify-between rounded-xl border border-moss/10 bg-white px-4 py-3"
                      >
                        <div>
                          <p className="font-semibold">{formatPhone(call.from_number)}</p>
                          <p className="text-xs text-moss/60">{call.intent ?? "No intent yet"}</p>
                        </div>
                        <StatusBadge status={call.status} />
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-moss/10 bg-white p-4 shadow-sm">
                <h3 className="mb-4 font-display text-xl font-semibold">Calls by status</h3>
                {view.statusDistribution.length === 0 ? (
                  <p className="text-sm text-moss/60">No call status data.</p>
                ) : (
                  <div className="h-64">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie data={view.statusDistribution} dataKey="value" nameKey="name" outerRadius={90}>
                          {view.statusDistribution.map((entry, index) => (
                            <Cell key={entry.name} fill={COLORS[index % COLORS.length]} />
                          ))}
                        </Pie>
                        <Tooltip />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>
            </section>

            <section className="rounded-xl border border-moss/10 bg-white p-4 shadow-sm">
              <h3 className="mb-4 font-display text-xl font-semibold">Performance (calls / day)</h3>
              {view.callsPerDay.length === 0 ? (
                <p className="text-sm text-moss/60">Not enough history for a chart yet.</p>
              ) : (
                <div className="h-72">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={view.callsPerDay}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis dataKey="date" />
                      <YAxis allowDecimals={false} />
                      <Tooltip />
                      <Bar dataKey="count" fill="#3d7a5f" radius={[6, 6, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </section>

            <section>
              <div className="mb-3 flex items-center justify-between">
                <h3 className="font-display text-xl font-semibold">Recent Calls</h3>
                <Link href="/calls" className="text-sm font-semibold text-leaf hover:underline">
                  Call history
                </Link>
              </div>
              <CallsTable calls={view.recent} emptyTitle="No recent calls." />
            </section>
          </div>
        )}
      </DataView>
    </AppShell>
  );
}
