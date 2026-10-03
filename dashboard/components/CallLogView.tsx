import { CallRecordingPlayer } from "@/components/CallRecordingPlayer";
import { StatusBadge } from "@/components/StatusBadge";
import { formatDuration, formatPhone, formatTime } from "@/lib/format";
import type { CallDetail } from "@/lib/types";

type TranscriptMessage = {
  role: "assistant" | "user" | "system" | "tool" | string;
  content: string;
};

function roleLabel(role: string): string {
  if (role === "user") return "USER";
  if (role === "assistant") return "AI";
  if (role === "tool") return "TOOL";
  return "SYSTEM";
}

export function CallLogView({
  call,
  messages,
}: {
  call: CallDetail;
  messages: TranscriptMessage[];
}) {
  const meta = [
    { label: "call id", value: call.id },
    { label: "caller", value: formatPhone(call.customer_phone || call.from_number) },
    { label: "receiver", value: formatPhone(call.to_number) },
    { label: "started", value: formatTime(call.started_at || call.created_at) },
    {
      label: "duration",
      value: formatDuration(call.duration_seconds ?? call.duration ?? null),
    },
    { label: "language", value: call.language || "—" },
  ];

  return (
    <section className="overflow-hidden rounded-xl border border-moss/10 bg-white">
      <header className="border-b border-moss/10 px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="font-display text-xl font-semibold tracking-tight">CallLog</h3>
          <StatusBadge status={call.status} />
        </div>
        <dl className="mt-4 grid gap-2 font-mono text-sm text-ink/90 sm:grid-cols-2">
          {meta.map((row) => (
            <div key={row.label} className="flex gap-2">
              <dt className="min-w-24 text-moss/55">{row.label}:</dt>
              <dd className="break-all">{row.value}</dd>
            </div>
          ))}
          <div className="flex gap-2 sm:col-span-2">
            <dt className="min-w-24 text-moss/55">status:</dt>
            <dd>{call.status}</dd>
          </div>
        </dl>

        <div className="mt-4">
          <h4 className="mb-2 font-display text-base font-semibold text-moss">Recording</h4>
          <CallRecordingPlayer callId={call.id} recording={call.recording} />
        </div>
      </header>

      <div className="px-5 py-4">
        <h4 className="mb-3 font-display text-base font-semibold text-moss">
          Conversation Transcript
        </h4>
        {messages.length === 0 ? (
          <p className="rounded-lg border border-dashed border-moss/20 bg-mist/60 px-4 py-6 text-sm text-moss/65">
            No transcript yet. Audio playback works independently of transcript text.
          </p>
        ) : (
          <ul className="space-y-3 font-mono text-sm">
            {messages.map((message, index) => {
              const label = roleLabel(message.role);
              const isUser = message.role === "user";
              return (
                <li key={`${message.role}-${index}`} className="flex gap-3">
                  <span
                    className={`mt-0.5 w-16 shrink-0 text-[11px] font-semibold uppercase tracking-wide ${
                      isUser ? "text-leaf" : "text-moss/70"
                    }`}
                  >
                    {label}
                  </span>
                  <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-ink">
                    {message.content}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
