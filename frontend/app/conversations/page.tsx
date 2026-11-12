"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type Citation = { document_id: string; label: string; excerpt: string; page: number };
type Turn = {
  seq: number;
  role: "user" | "assistant";
  text: string;
  abstained?: boolean;
  citations?: Citation[];
  conflicts?: unknown[];
};
type Summary = { id: string; project_id: string | null; updated_at: string; messages: number };
type Project = { id: string; name: string };

export default function Conversations() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [list, setList] = useState<Summary[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (organization: string) => {
    const res = await api(`/api/v1/conversations?organization_id=${organization}`);
    if (res.ok) setList((await res.json()) as Summary[]);
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
    const res = await api(`/api/v1/projects?organization_id=${first.id}`);
    if (res.ok) setProjects((await res.json()) as Project[]);
    await refresh(first.id);
  }, [router, refresh]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function open(id: string) {
    const res = await api(`/api/v1/conversations/${id}?organization_id=${org}`);
    if (!res.ok) {
      setMessage("The conversation could not be opened.");
      return;
    }
    setCurrent(id);
    setTurns(((await res.json()) as { turns: Turn[] }).turns);
  }

  async function start() {
    const res = await api(`/api/v1/conversations?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ project_id: project || null }),
    });
    if (res.status === 503) setMessage("Conversations are not enabled on this server.");
    else if (!res.ok) setMessage("The conversation could not be started.");
    else {
      const created = (await res.json()) as Summary;
      setCurrent(created.id);
      setTurns([]);
      await refresh(org);
    }
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!current) return;
    setBusy(true);
    setMessage("");
    const res = await api(`/api/v1/conversations/${current}/ask?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    setBusy(false);
    if (res.status === 409) setMessage("Another answer was saved at the same time. Reload and ask again.");
    else if (!res.ok) setMessage("The question could not be answered.");
    else {
      setQuestion("");
      await open(current);
      await refresh(org);
    }
  }

  async function remove(id: string) {
    const res = await api(`/api/v1/conversations/${id}?organization_id=${org}`, { method: "DELETE" });
    if (res.ok) {
      if (current === id) {
        setCurrent(null);
        setTurns([]);
      }
      await refresh(org);
    }
  }

  return (
    <main>
      <Nav current="conversations" />
      <section className="panel">
        <h1>Conversations</h1>
        <p>
          Your conversations are private to you and stored encrypted. Every answer cites the documents it comes
          from; earlier messages give context but are never used as a source.
        </p>
        <label>
          Case (optional)
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">All cases I can read</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <button onClick={start} disabled={!org}>
          New conversation
        </button>
        {message && <p role="status">{message}</p>}
        <ul>
          {list.map((c) => (
            <li key={c.id}>
              <button className="linkbutton" onClick={() => void open(c.id)}>
                {new Date(c.updated_at).toLocaleString()} · {c.messages} messages
              </button>{" "}
              <button onClick={() => void remove(c.id)}>Delete</button>
            </li>
          ))}
        </ul>
      </section>
      {current && (
        <section className="panel" aria-live="polite">
          {turns.map((t) => (
            <div key={t.seq} className="field">
              <strong>{t.role === "user" ? "You" : t.abstained ? "No answer found" : "Answer"}</strong>
              <p>{t.text}</p>
              {(t.citations ?? []).map((c) => (
                <blockquote key={`${t.seq}-${c.document_id}-${c.page}`}>
                  <Link href={`/documents/${c.document_id}`}>{c.label}</Link>
                  {c.excerpt && <>: {c.excerpt}</>}
                </blockquote>
              ))}
              {(t.conflicts ?? []).length > 0 && <small role="alert">Sources may disagree: check both.</small>}
            </div>
          ))}
          <form onSubmit={send}>
            <label>
              Question
              <input value={question} onChange={(e) => setQuestion(e.target.value)} minLength={3} maxLength={500} required />
            </label>
            <button disabled={busy}>{busy ? "Searching…" : "Ask"}</button>
          </form>
        </section>
      )}
    </main>
  );
}
