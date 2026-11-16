"use client";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type PlanSpec = {
  code: string;
  display_name: string;
  description: string;
  monthly_price_usd: number | null;
  annual_price_usd: number | null;
  max_users: number | null;
  max_documents: number | null;
  max_pages_per_month: number | null;
  max_queries_per_month: number | null;
  features: string[];
};
type UsageSnapshot = {
  period: string;
  plan: PlanSpec;
  subscription_status?: string;
  billing_cycle: string;
  consumption: Record<string, { used: number; limit: number | null; percent: number | null; status: string }>;
  alerts: { resource: string; status: string; used: number; limit: number }[];
};
type Statement = {
  period: string;
  plan_name: string;
  estimated_subscription_usd: number | null;
  estimated_ai_cost_usd: number;
  metrics: Record<string, number>;
};
type ApiKeyItem = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  revoked_at: string | null;
};
type ProviderInfo = {
  provider: string;
  display_name: string;
  recommended_for: string;
  supported_currencies: string[];
};
type BillingTx = {
  id: string;
  provider: string;
  external_tx_id: string;
  kind: string;
  plan_code: string;
  amount_cents: number;
  currency: string;
  status: string;
  created_at: string;
};

export default function Billing() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [plans, setPlans] = useState<PlanSpec[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [statements, setStatements] = useState<Statement[]>([]);
  const [keys, setKeys] = useState<ApiKeyItem[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [transactions, setTransactions] = useState<BillingTx[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("sandbox");
  const [selectedCurrency, setSelectedCurrency] = useState("USD");
  const [keyName, setKeyName] = useState("");
  const [includeDeleteScope, setIncludeDeleteScope] = useState(false);
  const [newSecret, setNewSecret] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (!first) return;
    setOrg(first.id);
    const [pRes, uRes, sRes, kRes, provRes, txRes] = await Promise.allSettled([
      api(`/api/v1/billing/plans?organization_id=${first.id}`),
      api(`/api/v1/billing/usage?organization_id=${first.id}`),
      api(`/api/v1/billing/statements?organization_id=${first.id}`),
      api(`/api/v1/api-keys?organization_id=${first.id}`),
      api(`/api/v1/billing/providers?organization_id=${first.id}`),
      api(`/api/v1/billing/transactions?organization_id=${first.id}`),
    ]);
    if (pRes.status === "fulfilled" && pRes.value.ok) {
      setPlans(((await pRes.value.json()) as { plans: PlanSpec[] }).plans);
    }
    if (uRes.status === "fulfilled" && uRes.value.ok) {
      setUsage((await uRes.value.json()) as UsageSnapshot);
    }
    if (sRes.status === "fulfilled" && sRes.value.ok) {
      setStatements((await sRes.value.json()) as Statement[]);
    }
    if (kRes.status === "fulfilled" && kRes.value.ok) {
      setKeys((await kRes.value.json()) as ApiKeyItem[]);
    }
    if (provRes.status === "fulfilled" && provRes.value.ok) {
      const body = (await provRes.value.json()) as {
        configured_provider: string;
        providers: ProviderInfo[];
      };
      setProviders(body.providers);
      setSelectedProvider(body.configured_provider || "sandbox");
    }
    if (txRes.status === "fulfilled" && txRes.value.ok) {
      const body = (await txRes.value.json()) as { transactions: BillingTx[] };
      setTransactions(body.transactions);
    }
  }, [router]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function selectPlan(code: string) {
    if (!org) return;
    const res = await api(`/api/v1/billing/subscription?organization_id=${org}`, {
      method: "PATCH",
      body: JSON.stringify({ plan: code }),
    });
    if (res.ok) {
      setMessage(`Workspace plan updated to ${code} (sandbox configuration).`);
      await load();
    } else if (res.status === 403) {
      setMessage("Only workspace owners can change the subscription plan.");
    } else {
      setMessage("Could not update subscription.");
    }
  }

  async function startCheckout(code: string) {
    if (!org) return;
    const res = await api(`/api/v1/billing/checkout?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({
        target_plan: code,
        billing_cycle: "monthly",
        provider: selectedProvider,
        currency: selectedCurrency,
      }),
    });
    if (res.ok) {
      const body = (await res.json()) as { external_tx_id: string; provider: string };
      setMessage(`Checkout session created (${body.provider}: ${body.external_tx_id}).`);
      await load();
    } else {
      const err = (await res.json().catch(() => ({}))) as { detail?: string };
      setMessage(`Checkout error: ${err.detail ?? "failed"}`);
    }
  }

  async function cancelSub() {
    if (!org) return;
    const res = await api(`/api/v1/billing/subscription/cancel?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ immediate: false }),
    });
    if (res.ok) {
      setMessage("Subscription scheduled for cancellation at period end.");
      await load();
    } else {
      setMessage("Could not cancel subscription.");
    }
  }

  async function createKey(e: FormEvent) {
    e.preventDefault();
    if (!org || !keyName.trim()) return;
    setNewSecret("");
    const scopes = ["documents:read", "documents:write", "exports:read"];
    if (includeDeleteScope) scopes.push("documents:delete");
    const res = await api(
      `/api/v1/api-keys?organization_id=${org}&name=${encodeURIComponent(keyName.trim())}`,
      {
        method: "POST",
        body: JSON.stringify(scopes),
      },
    );
    if (res.ok) {
      const body = (await res.json()) as { key: string };
      setNewSecret(body.key);
      setKeyName("");
      await load();
    } else setMessage("Could not create API key (admin role required).");
  }

  async function revokeKey(id: string) {
    const res = await api(`/api/v1/api-keys/${id}?organization_id=${org}`, { method: "DELETE" });
    if (res.ok) await load();
  }

  return (
    <main>
      <Nav current="billing" />
      <section className="panel wide-panel">
        <h1>SaaS plans &amp; consumption</h1>
        {message && <p role="status">{message}</p>}
        {usage ? (
          <>
            <p>
              Current plan: <strong>{usage.plan.display_name}</strong> · Status:{" "}
              <strong>{usage.subscription_status ?? "active"}</strong> · Period{" "}
              <strong>{usage.period}</strong>
            </p>
            {usage.alerts.length > 0 && (
              <p role="alert" className="error">
                Quota alert:{" "}
                {usage.alerts.map((a) => `${a.resource} (${a.used}/${a.limit})`).join(", ")}
              </p>
            )}
            {Object.entries(usage.consumption).map(([name, m]) => {
              const pct = m.percent !== null ? Math.min(100, m.percent) : 0;
              const cls = m.status === "exceeded" ? "danger" : m.status === "warning" ? "warn" : "";
              return (
                <div key={name} className="meter">
                  <div className="meter-head">
                    <span>{name.replace(/_/g, " ")}</span>
                    <span>
                      {m.used} / {m.limit ?? "Unlimited"}
                    </span>
                  </div>
                  {m.limit !== null && (
                    <div className="progress">
                      <span className={cls} style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
              );
            })}
            <div className="toolbar">
              <label>
                Gateway
                <select
                  value={selectedProvider}
                  onChange={(e) => setSelectedProvider(e.target.value)}
                >
                  {(providers.length ? providers : [{ provider: "sandbox", display_name: "Sandbox" }]).map(
                    (pr) => (
                      <option key={pr.provider} value={pr.provider}>
                        {pr.display_name}
                      </option>
                    ),
                  )}
                </select>
              </label>
              <label>
                Currency
                <select
                  value={selectedCurrency}
                  onChange={(e) => setSelectedCurrency(e.target.value)}
                >
                  <option value="USD">USD</option>
                  <option value="CLP">CLP (Chile)</option>
                  <option value="UF">UF (Chile)</option>
                </select>
              </label>
              <button type="button" className="secondary" onClick={() => void cancelSub()}>
                Cancel at period end
              </button>
            </div>
          </>
        ) : (
          <p>Administrator privileges are required to view billing metrics.</p>
        )}
      </section>

      {plans.length > 0 && (
        <section className="panel wide-panel">
          <h2>Available commercial plans (configurable)</h2>
          <div className="grid">
            {plans.map((p) => (
              <article key={p.code}>
                <h3>{p.display_name}</h3>
                <p>
                  <strong>
                    {p.monthly_price_usd !== null ? `$${p.monthly_price_usd}/mo` : "Custom pricing"}
                  </strong>
                </p>
                <p>{p.description}</p>
                <small>
                  Pages/mo: {p.max_pages_per_month ?? "Custom"} · Queries/mo:{" "}
                  {p.max_queries_per_month ?? "Custom"} · Users: {p.max_users ?? "Custom"}
                </small>
                <div className="toolbar">
                  <button
                    type="button"
                    disabled={usage?.plan.code === p.code}
                    onClick={() => void selectPlan(p.code)}
                  >
                    {usage?.plan.code === p.code ? "Active plan" : `Select ${p.display_name}`}
                  </button>
                  {p.code !== "FREE" && (
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void startCheckout(p.code)}
                    >
                      Checkout
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {transactions.length > 0 && (
        <section className="panel wide-panel">
          <h2>Transactions &amp; payment history</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Ref</th>
                  <th>Kind</th>
                  <th>Plan</th>
                  <th>Amount</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((t) => (
                  <tr key={t.id}>
                    <td>{t.provider}</td>
                    <td>{t.external_tx_id}</td>
                    <td>{t.kind}</td>
                    <td>{t.plan_code}</td>
                    <td>
                      {t.amount_cents} {t.currency}
                    </td>
                    <td>{t.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {statements.length > 0 && (
        <section className="panel wide-panel">
          <h2>Monthly statements</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Plan</th>
                  <th>Pages</th>
                  <th>Queries</th>
                  <th>AI Tokens</th>
                  <th>Est. Sub (USD)</th>
                </tr>
              </thead>
              <tbody>
                {statements.map((s) => (
                  <tr key={s.period}>
                    <td>{s.period}</td>
                    <td>{s.plan_name}</td>
                    <td>{s.metrics.pages_processed ?? 0}</td>
                    <td>{s.metrics.queries_executed ?? 0}</td>
                    <td>{s.metrics.ai_tokens_used ?? 0}</td>
                    <td>{s.estimated_subscription_usd ?? "Custom"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel wide-panel">
        <h2>API Keys</h2>
        <form onSubmit={createKey}>
          <label>
            Key name
            <input
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              placeholder="ERP integration"
              required
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={includeDeleteScope}
              onChange={(e) => setIncludeDeleteScope(e.target.checked)}
            />{" "}
            Grant granular <code>documents:delete</code> scope
          </label>
          <button>Create API key</button>
        </form>
        {newSecret && (
          <p>
            Copy your API key now (shown once): <code>{newSecret}</code>
          </p>
        )}
        <ul>
          {keys.map((k) => (
            <li key={k.id}>
              <span>
                <strong>{k.name}</strong> ({k.prefix}…) · {k.scopes.join(", ")}
              </span>
              {k.revoked_at ? (
                <span className="badge danger">Revoked</span>
              ) : (
                <button type="button" className="linkbutton" onClick={() => void revokeKey(k.id)}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
