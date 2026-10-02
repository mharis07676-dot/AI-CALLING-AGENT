import { statusTone } from "@/lib/format";

const tones: Record<ReturnType<typeof statusTone>, string> = {
  success: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  warning: "bg-amber-50 text-amber-800 ring-amber-200",
  danger: "bg-rose-50 text-rose-800 ring-rose-200",
  info: "bg-sky-50 text-sky-800 ring-sky-200",
  neutral: "bg-slate-50 text-slate-700 ring-slate-200",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = statusTone(status);
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold capitalize ring-1 ring-inset ${tones[tone]}`}
    >
      {status.replaceAll("_", " ")}
    </span>
  );
}
