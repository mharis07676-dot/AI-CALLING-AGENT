"use client";

import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { DataView, useAsyncData } from "@/components/useAsyncData";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function CampaignsPage() {
  const { data, error, loading, reload } = useAsyncData(() => api.getCampaigns(), []);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function createCampaign() {
    const name = window.prompt("Campaign name");
    if (!name?.trim()) return;
    setCreating(true);
    setActionError(null);
    try {
      await api.createCampaign({ name: name.trim() });
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setCreating(false);
    }
  }

  async function pause(id: string) {
    setBusyId(id);
    setActionError(null);
    try {
      await api.pauseCampaign(id);
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Pause failed");
    } finally {
      setBusyId(null);
    }
  }

  async function resume(id: string) {
    setBusyId(id);
    setActionError(null);
    try {
      await api.resumeCampaign(id);
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Resume failed");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <AppShell title="Campaigns" subtitle="Outbound dialing campaigns">
      {actionError ? (
        <div className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {actionError}
        </div>
      ) : null}
      <div className="mb-4 flex justify-end">
        <button
          type="button"
          disabled={creating}
          onClick={() => void createCampaign()}
          className="rounded-md bg-moss px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {creating ? "Creating…" : "+ New Campaign"}
        </button>
      </div>
      <DataView
        loading={loading}
        error={error}
        data={data}
        emptyTitle="No campaigns yet."
        emptyDescription="Create a campaign to track outbound dialing. Resume respects MAX_CONCURRENT_CALLS."
        onRetry={reload}
      >
        {(campaigns) =>
          campaigns.length === 0 ? (
            <EmptyState
              title="No campaigns yet."
              description="Create a campaign to track outbound dialing. Resume respects MAX_CONCURRENT_CALLS."
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white shadow-sm">
              <table className="min-w-[900px] w-full text-left text-sm">
                <thead className="bg-moss text-sand">
                  <tr>
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Contacts</th>
                    <th className="px-4 py-3">Queued</th>
                    <th className="px-4 py-3">Completed</th>
                    <th className="px-4 py-3">Failed</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {campaigns.map((campaign) => (
                    <tr key={campaign.id} className="border-t border-moss/10">
                      <td className="px-4 py-3 font-medium">{campaign.name}</td>
                      <td className="px-4 py-3">
                        <StatusBadge status={campaign.status} />
                      </td>
                      <td className="px-4 py-3">{campaign.total_contacts}</td>
                      <td className="px-4 py-3">{campaign.queued}</td>
                      <td className="px-4 py-3">{campaign.completed}</td>
                      <td className="px-4 py-3">{campaign.failed}</td>
                      <td className="px-4 py-3">{formatDateTime(campaign.created_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex gap-2">
                          <button
                            type="button"
                            disabled={busyId === campaign.id || campaign.status !== "running"}
                            onClick={() => void pause(campaign.id)}
                            className="rounded-md border border-moss/20 px-2.5 py-1.5 text-xs font-semibold disabled:opacity-40"
                          >
                            Pause
                          </button>
                          <button
                            type="button"
                            disabled={
                              busyId === campaign.id ||
                              (campaign.status !== "paused" && campaign.status !== "draft")
                            }
                            onClick={() => void resume(campaign.id)}
                            className="rounded-md border border-moss/20 px-2.5 py-1.5 text-xs font-semibold disabled:opacity-40"
                          >
                            Resume
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </DataView>
    </AppShell>
  );
}
