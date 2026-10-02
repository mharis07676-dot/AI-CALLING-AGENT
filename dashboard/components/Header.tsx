"use client";

import { useAuth } from "@/components/AuthProvider";

export function Header({ title, subtitle }: { title: string; subtitle?: string }) {
  const { user } = useAuth();

  return (
    <header className="border-b border-moss/10 bg-white/70 px-4 py-4 backdrop-blur sm:px-6 lg:px-8">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="font-display text-2xl font-semibold text-ink sm:text-3xl">{title}</h2>
          {subtitle ? <p className="mt-1 text-sm text-moss/70">{subtitle}</p> : null}
        </div>
        <p className="text-sm text-moss/60">
          Signed in as <span className="font-semibold text-ink">{user?.email ?? "…"}</span>
        </p>
      </div>
    </header>
  );
}
