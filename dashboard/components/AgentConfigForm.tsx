"use client";

import type { AgentConfig } from "@/lib/types";

export function AgentConfigForm({
  value,
  readOnly = true,
  onChange,
}: {
  value: AgentConfig;
  readOnly?: boolean;
  onChange?: (next: AgentConfig) => void;
}) {
  function update<K extends keyof AgentConfig>(key: K, fieldValue: AgentConfig[K]) {
    onChange?.({ ...value, [key]: fieldValue });
  }

  return (
    <div className="space-y-5 rounded-xl border border-moss/10 bg-white p-6 shadow-sm">
      {readOnly ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          Read-only mode.
        </div>
      ) : (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
          Editing safe business settings only. API keys and telephony secrets are never exposed here.
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Agent Name</span>
          <input
            value={value.agentName}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("agentName", e.target.value)}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Business Name</span>
          <input
            value={value.businessName}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("businessName", e.target.value)}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Voice</span>
          <input
            value={value.voice}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("voice", e.target.value)}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Max Call Duration (min)</span>
          <input
            type="number"
            value={value.maxCallDurationMinutes}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("maxCallDurationMinutes", Number(e.target.value))}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Max Clarification Attempts</span>
          <input
            type="number"
            value={value.maxClarificationAttempts}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("maxClarificationAttempts", Number(e.target.value))}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-semibold text-moss/70">Silence Timeout (sec)</span>
          <input
            type="number"
            value={value.silenceTimeoutSeconds}
            readOnly={readOnly}
            disabled={readOnly}
            onChange={(e) => update("silenceTimeoutSeconds", Number(e.target.value))}
            className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
          />
        </label>
      </div>

      <label className="block text-sm">
        <span className="mb-1 block font-semibold text-moss/70">Greeting</span>
        <textarea
          value={value.greeting}
          readOnly={readOnly}
          disabled={readOnly}
          rows={3}
          onChange={(e) => update("greeting", e.target.value)}
          className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 text-ink disabled:cursor-not-allowed"
        />
      </label>

      <fieldset className="text-sm">
        <legend className="mb-2 font-semibold text-moss/70">Supported Languages</legend>
        <div className="flex flex-wrap gap-4">
          {["English", "Urdu", "Roman Urdu"].map((lang) => (
            <label key={lang} className="inline-flex items-center gap-2">
              <input
                type="checkbox"
                checked={value.supportedLanguages.includes(lang)}
                disabled={readOnly}
                onChange={(e) => {
                  const next = e.target.checked
                    ? [...value.supportedLanguages, lang]
                    : value.supportedLanguages.filter((item) => item !== lang);
                  update("supportedLanguages", next);
                }}
              />
              {lang}
            </label>
          ))}
        </div>
      </fieldset>

      <label className="inline-flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={value.humanHandoffEnabled}
          disabled={readOnly}
          onChange={(e) => update("humanHandoffEnabled", e.target.checked)}
        />
        Human Handoff Enabled
      </label>

      <label className="block text-sm">
        <span className="mb-1 block font-semibold text-moss/70">System Instructions</span>
        <textarea
          value={value.systemInstructions}
          readOnly={readOnly}
          disabled={readOnly}
          rows={10}
          onChange={(e) => update("systemInstructions", e.target.value)}
          className="w-full rounded-md border border-moss/15 bg-mist/40 px-3 py-2 font-mono text-xs text-ink disabled:cursor-not-allowed"
        />
      </label>
    </div>
  );
}
