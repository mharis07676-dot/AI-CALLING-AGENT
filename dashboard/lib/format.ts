import type { Call, CallStatus, DashboardStats, Lead } from "./types";

export function formatPhone(value: string | null | undefined): string {
  return value?.trim() || "—";
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function isSameDay(value: string | null | undefined, day = new Date()): boolean {
  if (!value) return false;
  const date = new Date(value);
  return (
    date.getFullYear() === day.getFullYear() &&
    date.getMonth() === day.getMonth() &&
    date.getDate() === day.getDate()
  );
}

export function computeDashboardStats(
  calls: Call[],
  leads: Lead[],
  handoffCount: number,
  appointmentCount: number,
): DashboardStats {
  const activeStatuses: CallStatus[] = ["ringing", "active"];
  return {
    activeCalls: calls.filter((c) => activeStatuses.includes(c.status)).length,
    queuedCalls: calls.filter((c) => c.status === "queued").length,
    callsToday: calls.filter((c) => isSameDay(c.created_at) || isSameDay(c.started_at)).length,
    completedCalls: calls.filter((c) => c.status === "completed").length,
    failedCalls: calls.filter((c) => c.status === "failed" || c.status === "rejected").length,
    leadsToday: leads.filter((l) => isSameDay(l.created_at)).length,
    handoffs: handoffCount,
    appointments: appointmentCount,
  };
}

export function statusTone(status: string): "neutral" | "success" | "warning" | "danger" | "info" {
  const value = status.toLowerCase();
  if (["completed", "confirmed", "qualified", "converted", "success", "accepted"].includes(value)) {
    return "success";
  }
  if (["failed", "rejected", "cancelled", "lost", "critical", "error"].includes(value)) {
    return "danger";
  }
  if (["queued", "pending", "requested", "ringing", "warning"].includes(value)) {
    return "warning";
  }
  if (["active", "transferred", "contacted", "booked", "info"].includes(value)) {
    return "info";
  }
  return "neutral";
}
