"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type Citation = {
  document_id: string;
  document: string;
  page: number;
  page_basis: string;
  score: number;
  label: string;
  chunk_index: number | null;
  excerpt: string;
};
type ConflictSource = { document_id: string; document: string; page: number; text: string };
type Conflict = { kind: string; sources: ConflictSource[] };
type Answer = {
  answer: string;
  abstained: boolean;
  mode?: string;
  citations: Citation[];
  conflicts?: Conflict[];
};
type DocItem = { id: string; filename: string; status: string | null };
type HistoryItem = {
  id: string;
  question: string;
  answer_preview: string;
  abstained: boolean;
  created_at: string;
};

const SUGGESTIONS = [
  "What are the payment terms and due dates?",
  "What is the termination notice period?",
  "What total amounts or fees are specified?",
];

export default function Ask() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<Answer | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const loadHistory = useCallback(async (orgId: string) => {
    const res = await api(`/api/v1/ask/history?organization_id=${orgId}&limit=8`);
    if (res.ok) setHistory((await res.json()) as HistoryItem[]);
  }, []);

  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (!first) return;
    setOrg(first.id);
    const [docsRes] = await Promise.all([
      api(`/api/v1/documents?organization_id=${first.id}`),
      loadHistory(first.id),
    ]);
    if (docsRes.ok) setDocs((await docsRes.json()) as DocItem[]);
  }, [router, loadHistory]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  function toggleDoc(id: string) {
    setSelectedDocs((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    setResult(null);
    try {
      const payload: Record<string, unknown> = { question };
      if (selectedDocs.length > 0) payload.document_ids = selectedDocs;
      const res = await api(`/api/v1/ask?organization_id=${org}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (res.status === 402) {
        setMessage("Your plan's monthly query quota is exhausted.");
      } else if (res.status === 503) {
        setMessage("Question answering is not enabled on this server.");
      } else if (!res.ok) {
        setMessage("The question could not be answered.");
      } else {
        setResult((await res.json()) as Answer);
        await loadHistory(org);
      }
    } catch {
      setMessage("The question could not be answered.");
    } finally {
      setBusy(false);
    }
  }

  function exportAnswer() {
    if (!result) return;
    const blob = new Blob([JSON.stringify({ question, ...result }, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "answer-with-citations.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main>
      <Nav current="ask" />
      <section className="panel wide-panel">
        <h1>Ask your documents</h1>
        <p>
          Answers are grounded in your processed documents and always name the document and page
          they come from. If nothing relevant is found, the answer abstains instead of guessing.
        </p>
        <div className="chip-list">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              className="linkbutton"
              onClick={() => setQuestion(s)}
            >
              {s}
            </button>
          ))}
        </div>
        <form onSubmit={submit}>
          <label>
            Question
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              minLength={3}
              maxLength={500}
              required
            />
          </label>
          {docs.length > 0 && (
            <details>
              <summary>
                Scope to specific documents (
                {selectedDocs.length ? `${selectedDocs.length} selected` : "all accessible"})
              </summary>
              <ul>
                {docs.slice(0, 20).map((d) => (
                  <li key={d.id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selectedDocs.includes(d.id)}
                        onChange={() => toggleDoc(d.id)}
                      />{" "}
                      {d.filename}
                    </label>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <button disabled={busy || !org}>{busy ? "Searching…" : "Ask"}</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>

      {result && (
        <section className="panel wide-panel" aria-live="polite">
          <div className="viewerbar">
            <h2>{result.abstained ? "No answer found" : "Answer"}</h2>
            <button type="button" onClick={exportAnswer}>
              Export JSON
            </button>
          </div>
          <p>{result.answer}</p>
          {result.citations.length > 0 && (
            <>
              <h3>Sources</h3>
              <ul>
                {result.citations.map((c) => (
                  <li key={`${c.document_id}-${c.page}`}>
                    <Link href={`/documents/${c.document_id}`}>{c.label}</Link>
                    {c.chunk_index !== null && <small> · fragment {c.chunk_index + 1}</small>}
                    {c.excerpt && <blockquote>{c.excerpt}</blockquote>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {(result.conflicts ?? []).length > 0 && (
            <div role="alert">
              <h3>Sources that may disagree</h3>
              <p>
                These passages give different dates, figures or a negation for what looks like the
                same statement. Check both before relying on the answer.
              </p>
              <ul>
                {(result.conflicts ?? []).map((conflict, i) => (
                  <li key={i}>
                    {conflict.sources.map((s) => (
                      <blockquote key={`${s.document_id}-${s.page}`}>
                        <Link href={`/documents/${s.document_id}`}>
                          {s.document}, p.{s.page}
                        </Link>
                        : {s.text}
                      </blockquote>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {history.length > 0 && (
        <section className="panel wide-panel">
          <h2>Recent query history</h2>
          <ul>
            {history.map((h) => (
              <li key={h.id}>
                <span>
                  <strong>{h.question}</strong>
                  <br />
                  <small>{h.answer_preview}</small>
                </span>
                <span className="badge">{h.abstained ? "Abstained" : "Answered"}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
