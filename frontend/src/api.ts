// Thin client for the Sift backend. Every number in the UI comes from these calls.
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    // X-Sift marks the request as coming from this UI; the backend rejects state changes without it.
    headers: { "X-Sift": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* not json */ }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

export const api = {
  get: <T>(p: string) => req<T>("GET", p),
  post: <T>(p: string, b?: unknown) => req<T>("POST", p, b ?? {}),
  del: <T>(p: string) => req<T>("DELETE", p),
};

export interface Status {
  account: string | null;
  provider: string;
  connection: { state: "disconnected" | "connecting" | "connected" | "reauth_required" | "error"; error: string | null };
  can_modify: boolean;
  client_secrets_present: boolean | null;
  credential_store: string | null;
  synthetic: boolean;
  provider_name: string;
  classifier: { name: string; label: string; uses_ai: boolean };
  anthropic_api_key: string;
  first_run_cap: number;
  full_runs_unlocked: boolean;
  busy: boolean;
  mode: string;
}

export interface CategoryRow { category: string; label: string; total: number; inbox: number; unread: number; protected: number; keep: boolean }

export interface Dashboard {
  account: string | null;
  synthetic: boolean;
  scanned: number;
  sent_scanned: number;
  inbox: number;
  unread: number;
  promotional: number;
  subscriptions: number;
  protected: number;
  recommended: number;
  impact_pct: number;
  categories: CategoryRow[];
  top_unread_senders: { sender: string; display_name: string; unread: number; total: number; category: string }[];
  last_scan: { id: number; status: string; finished_at: string | null } | null;
}

export interface Scan {
  id: number;
  status: "running" | "cancelled" | "completed" | "failed" | "interrupted" | "none";
  phase: "sent" | "inbox" | "analyse" | "done";
  messages_scanned: number;
  senders_discovered: number;
  categories_detected: number;
  total_estimate: number | null;
  elapsed_s: number;
  errors: number;
  error_log: { at: string; error: string }[];
  resumable: boolean;
  max_per_phase: number | null;
  rate_mps: number | null;
  eta_s: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface SenderRow {
  sender: string;
  display_name: string;
  total: number;
  in_inbox: number;
  unread: number;
  eligible: number;
  protected_in_inbox: number;
  excluded: boolean;
  first_date: string | null;
  last_date: string | null;
  list_id: string | null;
  has_unsub: number;
  one_click_unsub: number;
  confidence: number;
  reason: string;
}

export interface MsgSample { id: string; from_addr: string; from_name: string; subject: string; internal_date: number; protect_reason?: string }

export interface Recommendation {
  id: string;
  category: string;
  category_label: string;
  title: string;
  action: "archive" | "trash" | "review";
  older_than_days: number;
  reason: string;
  confidence: number;
  classifier: string;
  count: number;
  sender_count: number;
  senders: SenderRow[];
  exceptions: { reason: string; count: number }[];
  protected_count: number;
  examples: MsgSample[];
  protected_examples: MsgSample[];
  date_range: [string | null, string | null];
  impact_pct: number;
  unsubscribe_one_click: number;
  unsubscribe_manual: number;
}

export interface PlanItemSummary {
  rec_id: string; action: string; older_than_days: number; excluded_senders: string[]; count: number;
  protected_excluded: number; protected_by_reason: Record<string, number>; too_recent: number; user_excluded: number;
}

export interface Plan {
  id: number;
  status: string;
  created_at: string;
  approved_at: string | null;
  total: number;
  ids_hash: string;
  inbox_before: number;
  spec: PlanItemSummary[];
  summary: { by_action: Record<string, number>; items: PlanItemSummary[]; protected_excluded: number; user_excluded: number; too_recent: number };
  message_status: Record<string, number>;
  samples: (MsgSample & { action: string; rec_id: string })[];
}

export interface Run {
  id: number;
  kind: "execute" | "restore";
  plan_id: number | null;
  source_run_id: number | null;
  status: "running" | "completed" | "partial" | "cancelled" | "failed";
  label_name: string | null;
  cap: number | null;
  total: number;
  attempted: number;
  confirmed: number;
  failed: number;
  skipped: number;
  already_done: number;
  current_op: string | null;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  audit_counts: Record<string, number>;
  skip_reasons: Record<string, number>;
  failures: { message_id: string; action: string; detail: string }[];
  plan?: { inbox_before: number; total: number; status: string } | null;
  plan_remaining?: number;
  inbox_now: number;
  restores: { id: number; status: string; confirmed: number; failed: number }[];
}
