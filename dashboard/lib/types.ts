export type CallDirection = "inbound" | "outbound";

export type CallStatus =
  | "ringing"
  | "active"
  | "queued"
  | "completed"
  | "failed"
  | "transferred"
  | "rejected";

export type LeadStatus =
  | "new"
  | "contacted"
  | "qualified"
  | "booked"
  | "handed_off"
  | "closed"
  | "lost";

export type Purpose = "rent" | "buy" | "sell" | "visit" | "general";

export type AppointmentStatus =
  | "pending"
  | "confirmed"
  | "cancelled"
  | "completed"
  | "no_show";

export type HandoffStatus = "requested" | "accepted" | "completed" | "cancelled";

export type UserRole = "owner" | "admin" | "agent" | "viewer";

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface User {
  id: string;
  tenant_id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
}

export interface LoginPayload {
  email: string;
  password: string;
  tenant_slug: string;
}

export interface Call {
  id: string;
  tenant_id: string;
  customer_id: string | null;
  customer_name?: string | null;
  customer_phone?: string | null;
  direction: CallDirection;
  status: CallStatus;
  from_number: string;
  to_number: string;
  provider_call_id: string | null;
  openai_session_id: string | null;
  intent: string | null;
  language?: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  failure_reason: string | null;
  created_at: string;
  recording?: CallRecording | null;
}

export interface CallMessage {
  id: string;
  role: "assistant" | "user" | "system" | "tool" | string;
  content: string;
  created_at: string;
}

export interface ToolExecution {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
  success: boolean;
  error: string | null;
  created_at: string;
}

export interface CallRecording {
  available?: boolean;
  status: string;
  format?: string | null;
  duration_seconds?: number | null;
  size_bytes?: number | null;
  playback_endpoint?: string | null;
  download_endpoint?: string | null;
}

export interface CallHandoffSummary {
  requested: boolean;
  status?: string | null;
  reason?: string | null;
  requested_at?: string | null;
  connected_at?: string | null;
  completed_at?: string | null;
}

export interface CallDetail extends Call {
  duration?: number | null;
  messages: CallMessage[];
  tool_executions: ToolExecution[];
  lead?: Lead | null;
  handoff?: HumanHandoff | null;
  handoff_summary?: CallHandoffSummary | null;
  handoff_requested?: boolean;
  handoff_status?: string | null;
  handoff_reason?: string | null;
  handoff_requested_at?: string | null;
  handoff_connected_at?: string | null;
  handoff_completed_at?: string | null;
  extracted_lead?: Record<string, unknown> | null;
  recording?: CallRecording | null;
}

export interface HangupResponse {
  success: boolean;
  call_id: string;
  status: CallStatus | null;
  message: string;
  provider_mode?: string | null;
  error?: string | null;
}

export interface Lead {
  id: string;
  tenant_id: string;
  customer_id: string | null;
  customer_name?: string | null;
  customer_phone?: string | null;
  call_id: string | null;
  purpose: Purpose;
  status: LeadStatus;
  location: string | null;
  property_type: string | null;
  size: string | null;
  budget_min: number | null;
  budget_max: number | null;
  notes: string | null;
  requirements: Record<string, unknown>;
  created_at: string;
}

export interface Appointment {
  id: string;
  tenant_id: string;
  customer_id: string;
  property_id: string | null;
  call_id: string | null;
  booking_code: string;
  scheduled_at: string;
  status: AppointmentStatus;
  notes: string | null;
}

export interface HumanHandoff {
  id: string;
  tenant_id: string;
  call_id: string;
  customer_id: string | null;
  customer_name?: string | null;
  customer_phone?: string | null;
  assigned_user_id: string | null;
  reason: string;
  status: HandoffStatus;
  context: Record<string, unknown>;
  resolution_notes?: string | null;
  created_at: string;
}

export interface DashboardStats {
  activeCalls: number;
  queuedCalls: number;
  callsToday: number;
  completedCalls: number;
  failedCalls: number;
  leadsToday: number;
  handoffs: number;
  appointments: number;
}

export interface AgentConfig {
  agentName: string;
  businessName: string;
  greeting: string;
  supportedLanguages: string[];
  voice: string;
  maxCallDurationMinutes: number;
  maxClarificationAttempts: number;
  humanHandoffEnabled: boolean;
  silenceTimeoutSeconds: number;
  systemInstructions: string;
}

export interface AnalyticsData {
  callsPerDay: { date: string; count: number }[];
  statusDistribution: { name: string; value: number }[];
  languageDistribution: { name: string; value: number }[];
  completedRate: number;
  failedRate: number;
  avgDurationSeconds: number;
  leadRate: number;
  handoffRate: number;
  appointmentRate: number;
}

export interface LogEvent {
  id: string;
  timestamp: string;
  level: "info" | "warning" | "error" | "critical";
  event: string;
  call_id: string | null;
  provider: string | null;
  message: string;
}

export interface Campaign {
  id: string;
  name: string;
  total_contacts: number;
  queued: number;
  dialing: number;
  in_progress: number;
  completed: number;
  no_answer: number;
  failed: number;
  interested: number;
  opted_out: number;
  created_at: string;
  status: "draft" | "running" | "paused" | "completed";
}

export class ApiError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string) {
    super(`API ${status}: ${body}`);
    this.status = status;
    this.body = body;
  }
}
