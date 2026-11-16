"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type Org = { id: string; name: string; role: string };
type Doc = {
  id: string;
  filename: string;
  pages: number | null;
  status: string | null;
  failure_code?: string | null;
  category?: string | null;
  project_id: string;
};
type ReviewItem = {
  document_id: string;
  filename: string;
  issue_count: number;
};
type QueryItem = {
  id: string;
  question: string;
  abstained: boolean;
  created_at: string;
};
type UsageMeter = { used: number; limit: number | null; percent: number | null; status: string };
type UsageSnapshot = {
  period: string;
  plan: { code: string; display_name: string };
  consumption: Record<string, UsageMeter>;
  alerts: { resource: string; status: string; used: number; limit: number }[];
};

export default function Dashboard() {
  const router = useRouter();
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [reviewQueue, setReviewQueue] = useState<ReviewItem[]>([]);
  const [queries, setQueries] = useState<QueryItem[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      const me = await api("/api/v1/auth/me");
      if (!me.ok) {
        router.replace("/login");
        return;
      }
      const response = await api("/api/v1/organizations");
      if (!response.ok) {
        setError("Could not load workspaces.");
        return;
      }
      const data = (await response.json()) as Org[];
      setOrgs(data);
      const lists = await Promise.all(
        data.map((x) =>
          api(`/api/v1/documents?organization_id=${x.id}`).then((r) => (r.ok ? r.json() : [])),
        ),
      );
      setDocs(lists.flat() as Doc[]);

      const primary = data[0];
      if (primary) {
        const [revRes, histRes, usageRes] = await Promise.allSettled([
          api(`/api/v1/review/queue?organization_id=${primary.id}`),
          api(`/api/v1/ask/history?organization_id=${primary.id}&limit=5`),
          api(`/api/v1/billing/usage?organization_id=${primary.id}`),
        ]);
        if (revRes.status === "fulfilled" && revRes.value.ok) {
          setReviewQueue((await revRes.value.json()) as ReviewItem[]);
        }
        if (histRes.status === "fulfilled" && histRes.value.ok) {
          setQueries((await histRes.value.json()) as QueryItem[]);
        }
        if (usageRes.status === "fulfilled" && usageRes.value.ok) {
          setUsage((await usageRes.value.json()) as UsageSnapshot);
        }
      }
    })();
  }, [router]);

  const completed = docs.filter((d) => d.status === "COMPLETED").length;
  const pending = docs.filter(
    (d) => d.status && !["COMPLETED", "FAILED", "CANCELLED"].includes(d.status),
  ).length;
  const failed = docs.filter((d) => d.status === "FAILED").length;

  return (
    <main>
      <Nav />
      <section className="dashboard">
        <div>
          <p className="eyebrow">WORKSPACE OVERVIEW</p>
          <h1>Documents, under control.</h1>
          <p>
            {error ||
              "Upload PDFs, Word, spreadsheets, presentations or scans, review extracted evidence and query across your workspace."}
          </p>
          <div className="toolbar">
            <Link className="button" href="/documents">
              Upload document
            </Link>
            <Link className="button" href="/ask">
              Ask documents
            </Link>
          </div>
        </div>
        <div className="stats">
          <article>
            <small>Workspaces</small>
            <b>{orgs.length}</b>
          </article>
          <article>
            <small>Completed</small>
            <b>{completed}</b>
          </article>
          <article>
            <small>Pending / Failed</small>
            <b>
              {pending} / {failed}
            </b>
          </article>
          <article>
            <small>Pages</small>
            <b>{docs.reduce((n, d) => n + (d.pages ?? 0), 0)}</b>
          </article>
          <article>
            <small>Needs review</small>
            <b>{reviewQueue.length}</b>
          </article>
          <article>
            <small>Plan</small>
            <b>{usage?.plan.display_name ?? (orgs[0]?.role || "—")}</b>
          </article>
        </div>
      </section>

      {usage && (
        <section className="panel wide-panel">
          <div className="viewerbar">
            <h2>
              Plan usage ({usage.plan.display_name} · {usage.period})
            </h2>
            <Link href="/billing">Manage plan &amp; limits</Link>
          </div>
          {usage.alerts.length > 0 && (
            <p role="alert" className="error">
              Consumption alert:{" "}
              {usage.alerts.map((a) => `${a.resource} (${a.used}/${a.limit})`).join(", ")}
            </p>
          )}
          {Object.entries(usage.consumption).map(([key, meter]) => {
            const pct = meter.percent !== null ? Math.min(100, meter.percent) : 0;
            const cls = meter.status === "exceeded" ? "danger" : meter.status === "warning" ? "warn" : "";
            return (
              <div key={key} className="meter">
                <div className="meter-head">
                  <span>{key.replace(/_/g, " ")}</span>
                  <span>
                    {meter.used} / {meter.limit ?? "Unlimited"}
                  </span>
                </div>
                {meter.limit !== null && (
                  <div className="progress">
                    <span className={cls} style={{ width: `${pct}%` }} />
                  </div>
                )}
              </div>
            );
          })}
        </section>
      )}

      <section className="panel wide-panel">
        <h2>Recent documents</h2>
        {docs.length ? (
          <ul>
            {docs.slice(0, 8).map((d) => (
              <li key={d.id}>
                <span>
                  <Link href={`/documents/${d.id}`}>{d.filename}</Link>{" "}
                  {d.category && <span className="badge">{d.category}</span>}
                </span>
                <span>
                  <span
                    className={`badge ${d.status === "FAILED" ? "danger" : d.status === "COMPLETED" ? "ok" : "warn"}`}
                  >
                    {d.status ?? "PENDING"}
                  </span>{" "}
                  {d.pages ?? "—"} pages
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p>No documents yet. Upload your first document to begin.</p>
        )}
      </section>

      {reviewQueue.length > 0 && (
        <section className="panel wide-panel">
          <h2>Pending human review</h2>
          <ul>
            {reviewQueue.slice(0, 5).map((item) => (
              <li key={item.document_id}>
                <Link href={`/documents/${item.document_id}`}>{item.filename}</Link>
                <span className="badge warn">{item.issue_count} flagged issue(s)</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {queries.length > 0 && (
        <section className="panel wide-panel">
          <h2>Recent questions</h2>
          <ul>
            {queries.map((q) => (
              <li key={q.id}>
                <span>{q.question}</span>
                <span className="badge">{q.abstained ? "Abstained" : "Cited answer"}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
