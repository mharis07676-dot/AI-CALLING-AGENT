import Link from "next/link";

const nav = [
  { href: "/", label: "Live Calls" },
  { href: "/leads", label: "Leads" },
  { href: "/appointments", label: "Appointments" },
  { href: "/handoffs", label: "Handoffs" },
  { href: "/properties", label: "Properties" },
];

const demoCalls = [
  { caller: "0300xxxxxxx", duration: "02:16", intent: "Rent", status: "AI talking" },
  { caller: "0321xxxxxxx", duration: "01:41", intent: "Buy", status: "Searching inventory" },
  { caller: "0333xxxxxxx", duration: "03:12", intent: "Visit", status: "Booking appointment" },
];

export default function HomePage() {
  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-6 py-8">
      <header className="mb-10 flex flex-col gap-6 border-b border-moss/15 pb-8 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="mb-2 text-sm font-semibold uppercase tracking-[0.2em] text-leaf">Synas Labs</p>
          <h1 className="font-display text-4xl font-bold tracking-tight text-ink md:text-5xl">
            Agent Ops
          </h1>
          <p className="mt-3 max-w-xl text-lg text-moss/80">
            Monitor live voice sessions, verified inventory answers, and human takeover — backend decides,
            AI only talks.
          </p>
        </div>
        <nav className="flex flex-wrap gap-3 text-sm font-semibold">
          {nav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="rounded-md bg-moss px-3 py-2 text-sand transition hover:bg-leaf"
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </header>

      <section className="mb-8 grid gap-4 md:grid-cols-3">
        {[
          { label: "Active calls", value: "3" },
          { label: "Open handoffs", value: "1" },
          { label: "Concurrency cap", value: "10" },
        ].map((stat) => (
          <div key={stat.label} className="border-l-4 border-leaf bg-sand/70 px-5 py-4">
            <p className="text-sm uppercase tracking-wide text-moss/70">{stat.label}</p>
            <p className="mt-1 font-display text-3xl font-bold">{stat.value}</p>
          </div>
        ))}
      </section>

      <section>
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="font-display text-2xl font-semibold">Live Calls</h2>
          <p className="text-sm text-moss/70">Demo data until API token is connected</p>
        </div>
        <div className="overflow-x-auto border border-moss/10 bg-white/70">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead className="bg-moss text-sand">
              <tr>
                <th className="px-4 py-3 font-semibold">Caller</th>
                <th className="px-4 py-3 font-semibold">Duration</th>
                <th className="px-4 py-3 font-semibold">Intent</th>
                <th className="px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {demoCalls.map((call) => (
                <tr key={call.caller} className="border-t border-moss/10">
                  <td className="px-4 py-3 font-medium">{call.caller}</td>
                  <td className="px-4 py-3">{call.duration}</td>
                  <td className="px-4 py-3">{call.intent}</td>
                  <td className="px-4 py-3 text-leaf">{call.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
