import type { Lead } from "@/lib/types";

export function LeadDetails({ lead }: { lead: Lead | null }) {
  if (!lead) {
    return (
      <div className="rounded-xl border border-dashed border-moss/20 bg-white px-5 py-8 text-sm text-moss/65">
        No extracted lead linked to this call yet.
      </div>
    );
  }

  const rows = [
    ["Purpose", lead.purpose],
    ["Location", lead.location ?? "—"],
    ["Property Type", lead.property_type ?? "—"],
    ["Size", lead.size ?? "—"],
    ["Budget", lead.budget_max != null ? String(lead.budget_max) : lead.budget_min != null ? String(lead.budget_min) : "—"],
    ["Status", lead.status],
  ];

  return (
    <dl className="grid gap-3 rounded-xl border border-moss/10 bg-white p-5 sm:grid-cols-2">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt className="text-xs font-semibold uppercase tracking-wide text-moss/55">{label}</dt>
          <dd className="mt-1 capitalize text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
