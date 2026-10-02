"use client";

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
import { StatCard } from "@/components/StatCard";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";

const COLORS = ["#3d7a5f", "#c45c26", "#1f3d32", "#64748b", "#0ea5e9", "#e11d48"];

export default function AnalyticsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getAnalytics(), []);

  return (
    <AppShell title="Analytics" subtitle="Calculated from tenant call/lead/appointment data">
      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No analytics data yet."
        onRetry={reload}
      >
        {(view) => (
          <div className="space-y-8">
            <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <StatCard label="Completed call rate" value={`${view.completedRate}%`} />
              <StatCard label="Failed call rate" value={`${view.failedRate}%`} />
              <StatCard label="Avg call duration (sec)" value={view.avgDurationSeconds} />
              <StatCard label="Lead rate" value={`${view.leadRate}%`} />
              <StatCard label="Handoff rate" value={`${view.handoffRate}%`} />
              <StatCard label="Appointment rate" value={`${view.appointmentRate}%`} />
            </section>

            <section className="grid gap-6 xl:grid-cols-2">
              <div className="rounded-xl border border-moss/10 bg-white p-4 shadow-sm">
                <h3 className="mb-4 font-display text-xl font-semibold">Calls per day</h3>
                {view.callsPerDay.length === 0 ? (
                  <p className="text-sm text-moss/60">No call history yet.</p>
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
              </div>

              <div className="rounded-xl border border-moss/10 bg-white p-4 shadow-sm">
                <h3 className="mb-4 font-display text-xl font-semibold">Call status distribution</h3>
                {view.statusDistribution.length === 0 ? (
                  <p className="text-sm text-moss/60">No status data.</p>
                ) : (
                  <div className="h-72">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie data={view.statusDistribution} dataKey="value" nameKey="name" outerRadius={95}>
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
              <h3 className="mb-4 font-display text-xl font-semibold">Language distribution</h3>
              {view.languageDistribution.length === 0 ? (
                <p className="text-sm text-moss/60">
                  No language data yet (populated when conversation records exist).
                </p>
              ) : (
                <div className="h-72">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={view.languageDistribution} dataKey="value" nameKey="name" outerRadius={95}>
                        {view.languageDistribution.map((entry, index) => (
                          <Cell key={entry.name} fill={COLORS[index % COLORS.length]} />
                        ))}
                      </Pie>
                      <Tooltip />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </section>
          </div>
        )}
      </DataView>
    </AppShell>
  );
}
