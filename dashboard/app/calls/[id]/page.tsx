"use client";

import { useParams } from "next/navigation";

import { AppShell } from "@/components/AppShell";
import { CallLogView } from "@/components/CallLogView";
import { LeadDetails } from "@/components/LeadDetails";
import { StatusBadge } from "@/components/StatusBadge";
import { ToolActivityList } from "@/components/ToolActivityList";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function CallDetailsPage() {
  const params = useParams<{ id: string }>();
  const callId = params.id;

  const { data, error, loading, reload } = useAsyncData(async () => {
    return api.getCallById(callId);
  }, [callId]);

  return (
    <AppShell title="Call Details" subtitle="Call log, conversation, and lead context">
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
          const summary = call.handoff_summary;
          const requested = Boolean(
            summary?.requested ?? call.handoff_requested ?? handoff,
          );
          const status = summary?.status ?? call.handoff_status ?? handoff?.status ?? null;
          const reason = summary?.reason ?? call.handoff_reason ?? handoff?.reason ?? null;
          const requestedAt =
            summary?.requested_at ?? call.handoff_requested_at ?? handoff?.created_at ?? null;
          const connectedAt = summary?.connected_at ?? call.handoff_connected_at ?? null;
          const messages = (call.messages || []).map((message) => ({
            role: message.role,
            content: message.content,
          }));

          return (
            <div className="space-y-8">
              <CallLogView call={call} messages={messages} />

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
                <dl className="grid gap-3 rounded-xl border border-moss/10 bg-white p-5 sm:grid-cols-2">
                  <div>
                    <dt className="text-xs font-semibold uppercase text-moss/55">Requested</dt>
                    <dd>{requested ? "Yes" : "No"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs font-semibold uppercase text-moss/55">Status</dt>
                    <dd>{status ? <StatusBadge status={status} /> : "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs font-semibold uppercase text-moss/55">Reason</dt>
                    <dd>{reason || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs font-semibold uppercase text-moss/55">Requested At</dt>
                    <dd>{formatDateTime(requestedAt)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs font-semibold uppercase text-moss/55">Connected At</dt>
                    <dd>{formatDateTime(connectedAt)}</dd>
                  </div>
                </dl>
              </section>
            </div>
          );
        }}
      </DataView>
    </AppShell>
  );
}
