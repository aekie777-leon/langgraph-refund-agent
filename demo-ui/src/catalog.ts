export type Persona = {
  id: string;
  label: string;
  role: "customer" | "support_agent" | "supervisor";
  tenant: string;
  token: string;
  note: string;
};

export const personas: Persona[] = [
  {
    id: "customer-a",
    label: "Maya · Customer",
    role: "customer",
    tenant: "tenant-demo",
    token: "showcase-customer-a",
    note: "Owns ORD-10001 through ORD-10012"
  },
  {
    id: "customer-b",
    label: "Noah · Customer",
    role: "customer",
    tenant: "tenant-demo",
    token: "showcase-customer-b",
    note: "Owns ORD-20001 only"
  },
  {
    id: "agent-7",
    label: "Ari · Support agent",
    role: "support_agent",
    tenant: "tenant-demo",
    token: "showcase-agent",
    note: "Reads only assigned cases"
  },
  {
    id: "sup-1",
    label: "Sam · Supervisor",
    role: "supervisor",
    tenant: "tenant-demo",
    token: "showcase-supervisor",
    note: "Tenant queue, assignment and Provider Ops"
  }
];

export type Scenario = {
  id: string;
  title: string;
  prompt: string;
  signal: string;
};

export const scenarios: Scenario[] = [
  {
    id: "refund",
    title: "Automatic refund",
    prompt: "Please refund ORD-10001.",
    signal: "Interrupt → deterministic eligibility → idempotent write"
  },
  {
    id: "manual-refund",
    title: "Manual review",
    prompt: "Please refund ORD-10002.",
    signal: "Amount threshold → support case"
  },
  {
    id: "cancel",
    title: "Provider lifecycle",
    prompt: "Please cancel ORD-10008.",
    signal: "Graph → Outbox → Provider → Inbox"
  },
  {
    id: "recovery",
    title: "Retry & recovery",
    prompt: "Please cancel ORD-10012.",
    signal: "HTTP 500 → scheduled retry → signed completion"
  },
  {
    id: "delivery",
    title: "Delivery investigation",
    prompt: "Tracking for ORD-10010 has not updated.",
    signal: "Deterministic stalled-tracking policy"
  },
  {
    id: "complaint",
    title: "Bilingual complaint",
    prompt: "我要正式投诉，客服辱骂了我。",
    signal: "Structured classification → immutable case event"
  },
  {
    id: "risk",
    title: "Risk-aware handoff",
    prompt: "I will contact consumer protection about ORD-10001.",
    signal: "Semantic risk → user-controlled priority interrupt"
  }
];
