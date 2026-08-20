const API_ROOT = "/api";

export type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  token: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers
    }
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as JsonObject;
      const error = body.error as JsonObject | undefined;
      message = String(error?.message ?? body.detail ?? message);
    } catch {
      // Keep the stable HTTP summary when the server did not return JSON.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export type Thread = { thread_id: string };

export function createThread(token: string): Promise<Thread> {
  return request<Thread>("/threads", token, {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function runThread(
  token: string,
  threadId: string,
  prompt: string
): Promise<JsonObject> {
  return request<JsonObject>(`/threads/${threadId}/runs/wait`, token, {
    method: "POST",
    body: JSON.stringify({
      assistant_id: "agent",
      input: { messages: [{ role: "user", content: prompt }] }
    })
  });
}

export function resumeThread(
  token: string,
  threadId: string,
  value: boolean | string
): Promise<JsonObject> {
  return request<JsonObject>(`/threads/${threadId}/runs/wait`, token, {
    method: "POST",
    body: JSON.stringify({
      assistant_id: "agent",
      command: { resume: value }
    })
  });
}

export type CaseRecord = {
  case_id: string;
  case_type: string;
  priority: string;
  status: string;
  order_id?: string | null;
  assigned_agent_id?: string | null;
  display_reason: string;
};

export type CasePage = { items: CaseRecord[]; total: number };

export function listCases(token: string): Promise<CasePage> {
  return request<CasePage>("/internal/support-cases?limit=50&offset=0", token);
}

export function assignCase(
  token: string,
  caseId: string,
  agentId: string
): Promise<JsonObject> {
  return request<JsonObject>(`/internal/support-cases/${caseId}/assign`, token, {
    method: "POST",
    body: JSON.stringify({
      agent_id: agentId,
      request_id: `showcase-assign-${caseId}-${agentId}`
    })
  });
}

export type QueueSummary = {
  status: string;
  count: number;
  oldest_available_at?: string | null;
};

export type QueueOverview = {
  outbox: QueueSummary[];
  inbox: QueueSummary[];
  generated_at: string;
};

export type AttemptActivity = {
  queue: "outbox" | "inbox";
  resource_id: string;
  command_id: string;
  cycle: number;
  attempt_number: number;
  outcome: string | null;
  failure_kind: string | null;
  http_status: number | null;
  safe_error_code: string | null;
  started_at: string;
  finished_at: string | null;
};

export type AttemptActivityFeed = {
  items: AttemptActivity[];
  generated_at: string;
};

export function getQueueOverview(token: string): Promise<QueueOverview> {
  return request<QueueOverview>("/internal/provider-operations/queues", token);
}

export function getAttemptActivity(token: string): Promise<AttemptActivityFeed> {
  return request<AttemptActivityFeed>(
    "/internal/provider-operations/attempts?limit=50",
    token
  );
}
