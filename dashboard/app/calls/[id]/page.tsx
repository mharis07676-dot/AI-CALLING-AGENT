"use client";

import { useParams } from "next/navigation";

import { AppShell } from "@/components/AppShell";
import { ConversationViewer } from "@/components/ConversationViewer";
import { LeadDetails } from "@/components/LeadDetails";
import { StatusBadge } from "@/components/StatusBadge";
import { ToolActivityList } from "@/components/ToolActivityList";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime, formatDuration, formatPhone } from "@/lib/format";

export default function CallDetailsPage() {
  const params = useParams<{ id: string }>();
  const callId = params.id;

  const { data, error, loading, reload } = useAsyncData(async () => {
    return api.getCallById(callId);
  }, [callId]);

  return (
    <AppShell title="Call Details" subtitle="Conversation, lead extraction, and handoff context">
      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="Call not found."
        emptyDescription="This call may not exist or belongs to another tenant."
        onRetry={reload}
      >
        {(call) => {
          const lead = call.lead ?? null;
          const handoff = call.handoff ?? null;
          const info = [
            ["Call ID", call.id],
            ["Customer", call.customer_name || call.customer_id || "—"],
            ["Phone", formatPhone(call.customer_phone || call.from_number)],
            ["Direction", call.direction],
            ["Status", call.status],
            ["Provider Call ID", call.provider_call_id ?? "—"],
            ["Started At", formatDateTime(call.started_at)],
            ["Ended At", formatDateTime(call.ended_at)],
            ["Duration", formatDuration(call.duration_seconds ?? call.duration ?? null)],
            ["Language", call.language ?? "—"],
            ["Intent", call.intent ?? "—"],
          ];

          return (
            <div className="space-y-8">
              <section>
                <div className="mb-3 flex items-center gap-3">
                  <h3 className="font-display text-xl font-semibold">Call Information</h3>
                  <StatusBadge status={call.status} />
                </div>
                <dl className="grid gap-3 rounded-xl border border-moss/10 bg-white p-5 sm:grid-cols-2 xl:grid-cols-3">
                  {info.map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-xs font-semibold uppercase tracking-wide text-moss/55">{label}</dt>
                      <dd className="mt-1 break-all text-sm text-ink">{value}</dd>
                    </div>
                  ))}
                </dl>
              </section>

              <section>
                <h3 className="mb-3 font-display text-xl font-semibold">Conversation</h3>
                <ConversationViewer
                  messages={(call.messages || []).map((message) => ({
                    role: message.role as "assistant" | "user" | "system" | "tool",
                    content: message.content,
                  }))}
                />
              </section>

              <section>
                <h3 className="mb-3 font-display text-xl font-semibold">Extracted Lead Information</h3>
                <LeadDetails lead={lead} />
              </section>

              <section>
                <h3 className="mb-3 font-display text-xl font-semibold">Tool Activity</h3>
                <ToolActivityList
                  items={(call.tool_executions || []).map((item) => ({
                    tool_name: item.tool_name,
                    success: item.success,
                    timestamp: item.created_at,
                    error: item.error,
                  }))}
                />
              </section>

              <section>
                <h3 className="mb-3 font-display text-xl font-semibold">Human Handoff</h3>
                {handoff ? (
                  <dl className="grid gap-3 rounded-xl border border-moss/10 bg-white p-5 sm:grid-cols-2">
                    <div>
                      <dt className="text-xs font-semibold uppercase text-moss/55">Requested</dt>
                      <dd>Yes</dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold uppercase text-moss/55">Status</dt>
                      <dd>
                        <StatusBadge status={handoff.status} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold uppercase text-moss/55">Reason</dt>
                      <dd>{handoff.reason}</dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold uppercase text-moss/55">Assigned staff</dt>
                      <dd>{handoff.assigned_user_id ?? "Unassigned"}</dd>
                    </div>
                  </dl>
                ) : (
                  <div className="rounded-xl border border-dashed border-moss/20 bg-white px-5 py-8 text-sm text-moss/65">
                    Human handoff not requested for this call.
                  </div>
                )}
              </section>
            </div>
          );
        }}
      </DataView>
    </AppShell>
  );
}
