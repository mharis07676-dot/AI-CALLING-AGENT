import { clearToken, getToken } from "./auth";
import type {
  AgentConfig,
  AnalyticsData,
  Appointment,
  Call,
  CallDetail,
  Campaign,
  DashboardStats,
  HangupResponse,
  HumanHandoff,
  Lead,
  LoginPayload,
  TokenResponse,
  User,
} from "./types";
import { ApiError } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

async function request<T>(path: string, init?: RequestInit, auth = true): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");

  if (auth) {
    const token = getToken();
    if (!token) {
      throw new ApiError(401, "Not authenticated");
    }
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
    throw new ApiError(401, "Unauthorized");
  }

  if (!res.ok) {
    throw new ApiError(res.status, await res.text());
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return res.json() as Promise<T>;
}

function mapAgentConfig(raw: Record<string, unknown>): AgentConfig {
  return {
    agentName: String(raw.agent_name ?? ""),
    businessName: String(raw.business_name ?? ""),
    greeting: String(raw.greeting ?? ""),
    supportedLanguages: Array.isArray(raw.supported_languages)
      ? (raw.supported_languages as string[])
      : [],
    voice: String(raw.voice ?? "alloy"),
    maxCallDurationMinutes: Number(raw.max_call_duration ?? 10),
    maxClarificationAttempts: Number(raw.max_clarification_attempts ?? 2),
    humanHandoffEnabled: Boolean(raw.human_handoff_enabled),
    silenceTimeoutSeconds: Number(raw.silence_timeout ?? 8),
    systemInstructions: String(raw.system_instructions ?? ""),
  };
}

function mapDashboardStats(raw: Record<string, number>): DashboardStats {
  return {
    activeCalls: raw.active_calls ?? 0,
    queuedCalls: raw.queued_calls ?? 0,
    callsToday: raw.calls_today ?? 0,
    completedCalls: raw.completed_calls ?? 0,
    failedCalls: raw.failed_calls ?? 0,
    leadsToday: raw.leads_today ?? 0,
    handoffs: raw.open_handoffs ?? 0,
    appointments: raw.appointments_today ?? 0,
  };
}

function mapAnalytics(raw: Record<string, unknown>): AnalyticsData {
  return {
    callsPerDay: (raw.calls_per_day as AnalyticsData["callsPerDay"]) ?? [],
    statusDistribution: (raw.status_distribution as AnalyticsData["statusDistribution"]) ?? [],
    languageDistribution: (raw.language_distribution as AnalyticsData["languageDistribution"]) ?? [],
    completedRate: Number(raw.completed_rate ?? 0),
    failedRate: Number(raw.failed_rate ?? 0),
    avgDurationSeconds: Number(raw.average_call_duration ?? 0),
    leadRate: Number(raw.lead_qualification_rate ?? 0),
    handoffRate: Number(raw.handoff_rate ?? 0),
    appointmentRate: Number(raw.appointment_booking_rate ?? 0),
  };
}

export const api = {
  login(payload: LoginPayload) {
    return request<TokenResponse>(
      "/auth/login",
      { method: "POST", body: JSON.stringify(payload) },
      false,
    );
  },

  me() {
    return request<User>("/auth/me");
  },

  getLiveCalls() {
    return request<Call[]>("/calls/live");
  },

  getCalls() {
    return request<Call[]>("/calls/");
  },

  getCallById(id: string) {
    return request<CallDetail>(`/calls/${id}`);
  },

  hangupCall(id: string) {
    return request<HangupResponse>(`/calls/${id}/hangup`, { method: "POST" });
  },

  createHandoff(payload: { call_id: string; reason: string; customer_id?: string }) {
    return request<HumanHandoff>("/calls/handoffs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getOpenHandoffs() {
    return request<HumanHandoff[]>("/calls/handoffs/open");
  },

  updateHandoff(
    id: string,
    payload: { status?: string; assigned_to?: string; resolution_notes?: string },
  ) {
    return request<HumanHandoff>(`/handoffs/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  getLeads() {
    return request<Lead[]>("/leads/");
  },

  updateLead(id: string, payload: Partial<Lead>) {
    return request<Lead>(`/leads/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  getAppointments() {
    return request<Appointment[]>("/appointments/");
  },

  getDashboardStats() {
    return request<Record<string, number>>("/dashboard/stats").then(mapDashboardStats);
  },

  getAnalytics() {
    return request<Record<string, unknown>>("/analytics").then(mapAnalytics);
  },

  getAgentConfig() {
    return request<Record<string, unknown>>("/agent/config").then(mapAgentConfig);
  },

  updateAgentConfig(payload: AgentConfig) {
    return request<Record<string, unknown>>("/agent/config", {
      method: "PUT",
      body: JSON.stringify({
        agent_name: payload.agentName,
        business_name: payload.businessName,
        greeting: payload.greeting,
        supported_languages: payload.supportedLanguages,
        voice: payload.voice,
        max_call_duration: payload.maxCallDurationMinutes,
        max_clarification_attempts: payload.maxClarificationAttempts,
        human_handoff_enabled: payload.humanHandoffEnabled,
        silence_timeout: payload.silenceTimeoutSeconds,
        system_instructions: payload.systemInstructions,
      }),
    }).then(mapAgentConfig);
  },

  getCampaigns() {
    return request<Campaign[]>("/campaigns/");
  },

  getCampaign(id: string) {
    return request<Campaign>(`/campaigns/${id}`);
  },

  createCampaign(payload: { name: string; total_contacts?: number }) {
    return request<Campaign>("/campaigns/", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  pauseCampaign(id: string) {
    return request<Campaign>(`/campaigns/${id}/pause`, { method: "POST" });
  },

  resumeCampaign(id: string) {
    return request<Campaign>(`/campaigns/${id}/resume`, { method: "POST" });
  },
};

export { API_URL };
