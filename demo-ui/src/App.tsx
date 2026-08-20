import { FormEvent, useMemo, useState } from "react";

import {
  ApiError,
  assignCase,
  createThread,
  getAttemptActivity,
  getQueueOverview,
  listCases,
  resumeThread,
  runThread,
  type CaseRecord,
  type AttemptActivity,
  type JsonObject,
  type QueueOverview
} from "./api";
import { personas, scenarios } from "./catalog";

type Workspace = "conversation" | "cases" | "provider";

function latestMessage(result: JsonObject | null): string {
  const messages = result?.messages;
  if (!Array.isArray(messages) || messages.length === 0) return "";
  const message = messages[messages.length - 1] as JsonObject;
  const content = message.content;
  return typeof content === "string" ? content : JSON.stringify(content ?? "");
}

function interruptValue(result: JsonObject | null): JsonObject | null {
  const interrupts = result?.__interrupt__;
  if (!Array.isArray(interrupts) || interrupts.length === 0) return null;
  const first = interrupts[0] as JsonObject;
  const value = first.value;
  return value && typeof value === "object" ? (value as JsonObject) : { value };
}

function resumeOptions(interrupt: JsonObject): Array<{ label: string; value: boolean | string }> {
  switch (interrupt.type) {
    case "human_handoff_confirmation":
      return [
        { label: "Confirm handoff", value: "confirm_handoff" },
        { label: "Continue self-service", value: "continue_self_service" }
      ];
    case "order_priority_confirmation":
      return [
        { label: "Handle the order", value: "handle_order" },
        { label: "Continue risk concern", value: "continue_risk" }
      ];
    default:
      return [
        { label: "Confirm", value: true },
        { label: "Cancel", value: false }
      ];
  }
}

export default function App() {
  const [personaId, setPersonaId] = useState(personas[0].id);
  const [workspace, setWorkspace] = useState<Workspace>("conversation");
  const [prompt, setPrompt] = useState(scenarios[0].prompt);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [result, setResult] = useState<JsonObject | null>(null);
  const [cases, setCases] = useState<CaseRecord[]>([]);
  const [queues, setQueues] = useState<QueueOverview | null>(null);
  const [attempts, setAttempts] = useState<AttemptActivity[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const persona = useMemo(
    () => personas.find((item) => item.id === personaId) ?? personas[0],
    [personaId]
  );
  const interrupt = interruptValue(result);

  async function execute(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.status} · ${caught.message}`
          : "The showcase request failed safely."
      );
    } finally {
      setBusy(false);
    }
  }

  function changePersona(next: string) {
    setPersonaId(next);
    setThreadId(null);
    setResult(null);
    setCases([]);
    setQueues(null);
    setAttempts([]);
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await execute(async () => {
      const activeThread = threadId ?? (await createThread(persona.token)).thread_id;
      setThreadId(activeThread);
      setResult(await runThread(persona.token, activeThread, prompt));
    });
  }

  async function resume(value: boolean | string) {
    if (!threadId) return;
    await execute(async () => setResult(await resumeThread(persona.token, threadId, value)));
  }

  async function refreshCases() {
    await execute(async () => setCases((await listCases(persona.token)).items));
  }

  async function assign(caseId: string, agentId: string) {
    await execute(async () => {
      await assignCase(persona.token, caseId, agentId);
      setCases((await listCases(persona.token)).items);
    });
  }

  async function refreshQueues() {
    await execute(async () => {
      const [overview, activity] = await Promise.all([
        getQueueOverview(persona.token),
        getAttemptActivity(persona.token)
      ]);
      setQueues(overview);
      setAttempts(activity.items);
    });
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="OpsPilot home">
          <span className="brand-mark">OP</span>
          <span><strong>OpsPilot</strong><small>Agent operations showcase</small></span>
        </a>
        <div className="environment"><span /> LOCAL SHOWCASE</div>
      </header>

      <main id="top">
        <section className="hero">
          <div>
            <p className="eyebrow">LANGGRAPH · POSTGRESQL · HUMAN-IN-THE-LOOP</p>
            <h1>Follow one decision<br />from message to audit trail.</h1>
            <p className="lede">A deterministic, multi-tenant customer-service system with explicit AI boundaries and recoverable Provider operations.</p>
          </div>
          <div className="hero-proof" aria-label="System guarantees">
            <div><strong>2</strong><span>fenced workers</span></div>
            <div><strong>3</strong><span>RBAC roles</span></div>
            <div><strong>0</strong><span>production calls</span></div>
          </div>
        </section>

        <section className="control-row" aria-label="Showcase controls">
          <label>
            <span>Acting persona</span>
            <select value={personaId} onChange={(event) => changePersona(event.target.value)}>
              {personas.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>
          <div className="identity-card">
            <span className={`role-dot ${persona.role}`} />
            <div><strong>{persona.role}</strong><small>{persona.tenant} · {persona.note}</small></div>
          </div>
          <div className="privacy-note"><strong>Trust boundary</strong><span>The UI supplies a demo Bearer token. The backend still derives tenant, role and permissions.</span></div>
        </section>

        <nav className="tabs" aria-label="Workspace">
          {(["conversation", "cases", "provider"] as Workspace[]).map((tab) => (
            <button key={tab} className={workspace === tab ? "active" : ""} onClick={() => setWorkspace(tab)}>
              {tab === "conversation" ? "Agent run" : tab === "cases" ? "Case queue" : "Provider Ops"}
            </button>
          ))}
        </nav>

        {error && <div className="error-banner" role="alert">{error}</div>}

        {workspace === "conversation" && (
          <section className="workspace-grid">
            <aside className="scenario-list">
              <p className="section-label">Prepared evidence paths</p>
              {scenarios.map((scenario) => (
                <button key={scenario.id} onClick={() => setPrompt(scenario.prompt)} className={prompt === scenario.prompt ? "selected" : ""}>
                  <strong>{scenario.title}</strong><span>{scenario.signal}</span>
                </button>
              ))}
            </aside>
            <article className="run-panel">
              <div className="panel-heading"><div><p className="section-label">Thread-scoped execution</p><h2>Customer conversation</h2></div><code>{threadId ? threadId.slice(0, 13) + "…" : "new thread"}</code></div>
              <form onSubmit={submit}>
                <label htmlFor="prompt">Synthetic customer message</label>
                <textarea id="prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={4} />
                <button className="primary" disabled={busy || !prompt.trim()}>{busy ? "Running…" : "Run through Graph"}</button>
              </form>
              {interrupt && (
                <div className="interrupt-card">
                  <span className="pulse" /><div><p className="section-label">Execution paused</p><h3>{String(interrupt.type ?? "Human decision required")}</h3><p>{String(interrupt.question ?? interrupt.message ?? "The Graph saved state and is waiting on the same thread.")}</p>
                  <div className="button-row">{resumeOptions(interrupt).map((option) => <button key={String(option.value)} onClick={() => resume(option.value)} disabled={busy}>{option.label}</button>)}</div></div>
                </div>
              )}
              {latestMessage(result) && <div className="result-card"><p className="section-label">Latest assistant output</p><p>{latestMessage(result)}</p></div>}
              {result && <details><summary>Inspect safe Graph state</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
            </article>
          </section>
        )}

        {workspace === "cases" && (
          <section className="data-panel">
            <div className="panel-heading"><div><p className="section-label">Tenant-scoped control plane</p><h2>Support cases</h2></div><button onClick={refreshCases} disabled={busy}>Refresh queue</button></div>
            {cases.length === 0 ? <Empty text="Run a handoff scenario, switch to the supervisor, then refresh." /> : (
              <div className="card-grid">{cases.map((item) => <article className="case-card" key={item.case_id}><div className="case-meta"><span>{item.priority}</span><span>{item.status}</span></div><h3>{item.case_type.replaceAll("_", " ")}</h3><p>{item.display_reason}</p><dl><div><dt>Order</dt><dd>{item.order_id ?? "—"}</dd></div><div><dt>Assigned</dt><dd>{item.assigned_agent_id ?? "Unassigned"}</dd></div></dl>{persona.role === "supervisor" && <div className="button-row"><button onClick={() => assign(item.case_id, "agent-7")}>Assign active agent</button><button onClick={() => assign(item.case_id, "missing-agent")}>Test safe failure</button></div>}</article>)}</div>
            )}
          </section>
        )}

        {workspace === "provider" && (
          <section className="data-panel">
            <div className="panel-heading"><div><p className="section-label">Payload-free operations</p><h2>Provider queues</h2></div><button onClick={refreshQueues} disabled={busy}>Refresh queues</button></div>
            {!queues ? <Empty text="Supervisor permission is required. Responses expose operational metadata only." /> : <><div className="queue-columns"><Queue title="Outbox" items={queues.outbox} /><Queue title="Inbox" items={queues.inbox} /></div><AttemptTimeline items={attempts} /></>}
          </section>
        )}
      </main>
      <footer><span>LangGraph Refund Agent · v1.0 showcase</span><span>Development-only · synthetic data</span></footer>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="empty-state"><span>◇</span><p>{text}</p></div>;
}

function Queue({ title, items }: { title: string; items: QueueOverview["outbox"] }) {
  return <article className="queue-card"><h3>{title}</h3>{items.length === 0 ? <p className="muted">No queue records.</p> : items.map((item) => <div className="queue-row" key={item.status}><span>{item.status}</span><strong>{item.count}</strong></div>)}</article>;
}

function AttemptTimeline({ items }: { items: AttemptActivity[] }) {
  return <section className="attempt-panel" aria-label="Provider attempt timeline"><div><p className="section-label">Bounded audit projection</p><h3>Recent delivery attempts</h3><p>Retry evidence is read from PostgreSQL. Payloads, customer data and Provider references stay hidden.</p></div>{items.length === 0 ? <p className="muted">No delivery attempts yet.</p> : <div className="attempt-list">{items.map((item) => <article className="attempt-row" key={`${item.queue}-${item.resource_id}-${item.cycle}-${item.attempt_number}`}><span className={`attempt-marker ${item.outcome ?? "running"}`} /><div><strong>{item.queue} · cycle {item.cycle} · attempt {item.attempt_number}</strong><small>{item.command_id.slice(0, 13)}…</small></div><div className="attempt-outcome"><strong>{item.outcome ?? "running"}</strong><small>{item.http_status ? `HTTP ${item.http_status}` : item.safe_error_code ?? "structured result"}</small></div></article>)}</div>}</section>;
}
