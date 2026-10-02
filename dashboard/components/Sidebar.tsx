"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/components/AuthProvider";

const nav = [
  { href: "/dashboard", label: "Dashboard" },
  {
    label: "Calls",
    children: [
      { href: "/live-calls", label: "Live Calls" },
      { href: "/calls", label: "Call History" },
    ],
  },
  { href: "/leads", label: "Leads" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/appointments", label: "Appointments" },
  { href: "/handoffs", label: "Handoffs" },
  { href: "/agent", label: "Agent Configuration" },
  { href: "/analytics", label: "Analytics" },
  { href: "/logs", label: "Logs" },
  { href: "/settings", label: "Settings" },
];

function NavLink({
  href,
  label,
  onNavigate,
}: {
  href: string;
  label: string;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const active = pathname === href || pathname.startsWith(`${href}/`);
  return (
    <Link
      href={href}
      onClick={onNavigate}
      className={`block rounded-md px-3 py-2 text-sm font-medium transition ${
        active ? "bg-leaf text-white" : "text-sand/85 hover:bg-white/10 hover:text-white"
      }`}
    >
      {label}
    </Link>
  );
}

export function Sidebar() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);

  const content = (
    <div className="flex h-full flex-col">
      <div className="border-b border-white/10 px-5 py-6">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-leaf">Synas Labs</p>
        <h1 className="mt-1 font-display text-2xl font-semibold text-white">AI Agents</h1>
      </div>

      <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
        {nav.map((item) =>
          "children" in item && item.children ? (
            <div key={item.label} className="pt-2">
              <p className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-white/45">
                {item.label}
              </p>
              <div className="space-y-1">
                {item.children.map((child) => (
                  <NavLink
                    key={child.href}
                    href={child.href}
                    label={child.label}
                    onNavigate={() => setOpen(false)}
                  />
                ))}
              </div>
            </div>
          ) : (
            <NavLink
              key={item.href}
              href={item.href!}
              label={item.label}
              onNavigate={() => setOpen(false)}
            />
          ),
        )}
      </nav>

      <div className="border-t border-white/10 px-4 py-4">
        <div className="mb-3 rounded-md bg-white/5 px-3 py-2">
          <p className="truncate text-sm font-semibold text-white">{user?.full_name ?? "Admin"}</p>
          <p className="truncate text-xs text-white/55">{user?.email ?? "—"}</p>
        </div>
        <button
          type="button"
          onClick={logout}
          className="w-full rounded-md border border-white/15 px-3 py-2 text-sm font-medium text-white/90 transition hover:bg-white/10"
        >
          Logout
        </button>
      </div>
    </div>
  );

  return (
    <>
      <button
        type="button"
        className="fixed left-4 top-4 z-40 rounded-md bg-moss px-3 py-2 text-sm font-semibold text-white shadow lg:hidden"
        onClick={() => setOpen(true)}
      >
        Menu
      </button>

      {open ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-black/40"
            onClick={() => setOpen(false)}
          />
          <aside className="absolute left-0 top-0 h-full w-72 bg-moss shadow-xl">{content}</aside>
        </div>
      ) : null}

      <aside className="hidden w-72 shrink-0 bg-moss lg:block">{content}</aside>
    </>
  );
}
