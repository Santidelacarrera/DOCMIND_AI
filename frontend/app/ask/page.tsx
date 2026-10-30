"use client";
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
};
type Answer = { answer: string; abstained: boolean; citations: Citation[] };

export default function Ask() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<Answer | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (first) setOrg(first.id);
  }, [router]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    setResult(null);
    try {
      const res = await api(`/api/v1/ask?organization_id=${org}`, {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      if (res.status === 503) {
        setMessage("Question answering is not enabled on this server.");
      } else if (!res.ok) {
        setMessage("The question could not be answered.");
      } else {
        setResult((await res.json()) as Answer);
      }
    } catch {
      setMessage("The question could not be answered.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <Nav current="ask" />
      <section className="panel">
        <h1>Ask your documents</h1>
        <p>
          Answers are quoted from your processed documents and always name the document and page they
          come from. If nothing relevant is found, the answer says so instead of guessing.
        </p>
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
          <button disabled={busy || !org}>{busy ? "Searching…" : "Ask"}</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>
      {result && (
        <section className="panel" aria-live="polite">
          <h2>{result.abstained ? "No answer found" : "Answer"}</h2>
          <p>{result.answer}</p>
          {result.citations.length > 0 && (
            <>
              <h3>Sources</h3>
              <ul>
                {result.citations.map((c) => (
                  <li key={`${c.document_id}-${c.page}`}>
                    {c.label}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}
    </main>
  );
}
