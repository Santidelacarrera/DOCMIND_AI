"use client";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { API_URL, api } from "../../lib/api";

type Doc = { id: string; filename: string; status: string | null };
type Project = { id: string; name: string };
type FieldCmp = {
  field: string;
  status: string;
  values: { document_id: string; filename: string; value: unknown; page: number | null }[];
};
type Conflict = {
  kind: string;
  sources: { document: string; page: number; text: string }[];
};
type Comparison = {
  documents: { document_id: string; filename: string }[];
  summary: {
    documents_compared: number;
    fields_compared: number;
    differences_found: number;
    conflicts_found: number;
  };
  field_comparisons: FieldCmp[];
  conflicts: Conflict[];
};

export default function Intelligence() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [docs, setDocs] = useState<Doc[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [topic, setTopic] = useState("");
  const [report, setReport] = useState<Comparison | null>(null);
  const [projectId, setProjectId] = useState("");
  const [projectSummary, setProjectSummary] = useState<{ summary: string; documents_included: number } | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (!first) return;
    setOrg(first.id);
    const [dRes, pRes] = await Promise.all([
      api(`/api/v1/documents?organization_id=${first.id}`),
      api(`/api/v1/projects?organization_id=${first.id}`),
    ]);
    if (dRes.ok) setDocs((await dRes.json()) as Doc[]);
    if (pRes.ok) {
      const pList = (await pRes.json()) as Project[];
      setProjects(pList);
      setProjectId(pList[0]?.id ?? "");
    }
  }, [router]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  function toggle(id: string) {
    setSelected((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length < 5
          ? [...prev, id]
          : prev,
    );
  }

  async function runCompare(e: FormEvent) {
    e.preventDefault();
    if (selected.length < 2) {
      setMessage("Select at least 2 documents to compare.");
      return;
    }
    setBusy(true);
    setMessage("");
    const res = await api(`/api/v1/documents/compare?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ document_ids: selected, topic }),
    });
    setBusy(false);
    if (res.ok) setReport((await res.json()) as Comparison);
    else setMessage("Could not compare the selected documents.");
  }

  async function downloadCompare(format: "csv" | "xlsx") {
    if (!org || selected.length < 2) return;
    const res = await fetch(
      `${API_URL}/api/v1/documents/compare?organization_id=${org}&format=${format}`,
      {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: selected, topic }),
      },
    );
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `comparison.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function summarizeProject() {
    if (!org || !projectId) return;
    const res = await api(`/api/v1/projects/${projectId}/summarize?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ max_sentences: 6 }),
    });
    if (res.ok) setProjectSummary((await res.json()) as { summary: string; documents_included: number });
  }

  return (
    <main>
      <Nav current="intelligence" />
      <section className="panel wide-panel">
        <h1>Document comparison &amp; contradiction detection</h1>
        <p>
          Pick 2 to 5 processed documents to compare extracted fields side by side and detect
          conflicting dates, figures or clauses.
        </p>
        <form onSubmit={runCompare}>
          <label>
            Focus topic (optional)
            <input
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="renewal fee, termination notice, dates…"
            />
          </label>
          <ul>
            {docs.map((d) => (
              <li key={d.id}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.includes(d.id)}
                    onChange={() => toggle(d.id)}
                  />{" "}
                  {d.filename}
                </label>
                <span className="badge">{d.status ?? "PENDING"}</span>
              </li>
            ))}
          </ul>
          <button disabled={busy || selected.length < 2}>
            {busy ? "Comparing…" : "Compare selected"}
          </button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>

      {report && (
        <section className="panel wide-panel">
          <div className="viewerbar">
            <h2>
              Comparison ({report.summary.differences_found} field difference(s),{" "}
              {report.summary.conflicts_found} textual conflict(s))
            </h2>
            <span>
              <button type="button" onClick={() => void downloadCompare("csv")}>
                CSV
              </button>
              <button type="button" onClick={() => void downloadCompare("xlsx")}>
                XLSX
              </button>
            </span>
          </div>
          {report.field_comparisons.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Status</th>
                    {report.documents.map((d) => (
                      <th key={d.document_id}>{d.filename}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {report.field_comparisons.map((fc) => (
                    <tr key={fc.field}>
                      <td>{fc.field}</td>
                      <td>
                        <span className={`badge ${fc.status === "identical" ? "ok" : "warn"}`}>
                          {fc.status}
                        </span>
                      </td>
                      {fc.values.map((v) => (
                        <td key={v.document_id}>{String(v.value ?? "—")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {report.conflicts.length > 0 && (
            <>
              <h3>Textual contradictions</h3>
              <ul>
                {report.conflicts.map((c, idx) => (
                  <li key={idx}>
                    {c.sources.map((s, j) => (
                      <blockquote key={j}>
                        <strong>
                          {s.document}, p.{s.page}
                        </strong>
                        : {s.text}
                      </blockquote>
                    ))}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {projects.length > 0 && (
        <section className="panel wide-panel">
          <h2>Multi-document project summary</h2>
          <div className="toolbar">
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <button type="button" onClick={() => void summarizeProject()}>
              Summarize project
            </button>
          </div>
          {projectSummary && (
            <p>
              <strong>From {projectSummary.documents_included} document(s):</strong>{" "}
              {projectSummary.summary}
            </p>
          )}
        </section>
      )}
    </main>
  );
}
