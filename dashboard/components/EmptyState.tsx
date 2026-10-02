export function EmptyState({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="rounded-xl border border-dashed border-moss/20 bg-white/70 px-6 py-10 text-center">
      <p className="font-display text-lg font-semibold text-ink">{title}</p>
      {description ? <p className="mt-2 text-sm text-moss/65">{description}</p> : null}
    </div>
  );
}
