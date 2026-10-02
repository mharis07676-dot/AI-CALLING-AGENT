export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex min-h-[180px] flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-moss/20 bg-white/60 p-8">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-leaf border-t-transparent" />
      <p className="text-sm text-moss/70">{label}</p>
    </div>
  );
}
