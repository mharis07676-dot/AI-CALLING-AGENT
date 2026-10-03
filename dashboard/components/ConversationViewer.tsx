export function ConversationViewer({
  messages,
}: {
  messages: { role: "assistant" | "user" | "system" | "tool"; content: string }[];
}) {
  if (messages.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-moss/20 bg-white px-5 py-8 text-sm text-moss/65">
        No transcript yet. Messages appear once Realtime turn transcripts are saved for this call.
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-xl border border-moss/10 bg-white p-4">
      {messages.map((message, index) => {
        const isAi = message.role === "assistant" || message.role === "system";
        return (
          <div key={`${message.role}-${index}`} className={`flex ${isAi ? "justify-start" : "justify-end"}`}>
            <div
              className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm ${
                isAi ? "bg-mist text-ink" : "bg-leaf text-white"
              }`}
            >
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide opacity-70">
                {isAi ? "AI" : "USER"}
              </p>
              <p>{message.content}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
