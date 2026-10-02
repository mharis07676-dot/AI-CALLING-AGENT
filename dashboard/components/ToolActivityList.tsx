import { StatusBadge } from "@/components/StatusBadge";

export interface ToolActivityItem {
  tool_name: string;
  success: boolean;
  timestamp?: string;
  error?: string | null;
}

export function ToolActivityList({ items }: { items: ToolActivityItem[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-moss/20 bg-white px-5 py-8 text-sm text-moss/65">
        Tool activity unavailable. Backend has no tool-call history API yet.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-moss/10 bg-white">
      <table className="min-w-[560px] w-full text-left text-sm">
        <thead className="bg-moss text-sand">
          <tr>
            <th className="px-4 py-3">Tool</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Timestamp</th>
            <th className="px-4 py-3">Error</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, index) => (
            <tr key={`${item.tool_name}-${index}`} className="border-t border-moss/10">
              <td className="px-4 py-3 font-medium">{item.tool_name}</td>
              <td className="px-4 py-3">
                <StatusBadge status={item.success ? "success" : "failed"} />
              </td>
              <td className="px-4 py-3">{item.timestamp ?? "—"}</td>
              <td className="px-4 py-3 text-rose-700">{item.error ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
