"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
type Org = { id: string; name: string; role: string };
type Doc = {
  id: string;
  filename: string;
  pages: number | null;
  project_id: string;
};
export default function Dashboard() {
  const router = useRouter();
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [docs, setDocs] = useState<Doc[]>([]);
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
          api(`/api/v1/documents?organization_id=${x.id}`).then((r) =>
            r.ok ? r.json() : [],
          ),
        ),
      );
      setDocs(lists.flat() as Doc[]);
    })();
  }, [router]);
  return (
    <main>
      <nav>
        <strong>DocMind AI</strong>
        <Link href="/documents">Documents</Link>
        <Link href="/schemas">Schemas</Link>
      </nav>
      <section className="dashboard">
        <div>
          <p className="eyebrow">WORKSPACE OVERVIEW</p>
          <h1>Documents, under control.</h1>
          <p>
            {error ||
              "Track processing and review structured data from your PDFs."}
          </p>
          <Link className="button" href="/documents">
            Upload document
          </Link>
        </div>
        <div className="stats">
          <article>
            <small>Workspaces</small>
            <b>{orgs.length}</b>
          </article>
          <article>
            <small>Documents</small>
            <b>{docs.length}</b>
          </article>
          <article>
            <small>Pages processed</small>
            <b>{docs.reduce((n, d) => n + (d.pages ?? 0), 0)}</b>
          </article>
        </div>
      </section>
      <section className="panel">
        <h2>Recent documents</h2>
        {docs.length ? (
          <ul>
            {docs.slice(0, 8).map((d) => (
              <li key={d.id}>
                <Link href={`/documents/${d.id}`}>{d.filename}</Link>
                <span>{d.pages ?? "Processing"} pages</span>
              </li>
            ))}
          </ul>
        ) : (
          <p>No documents yet. Upload your first PDF to begin.</p>
        )}
      </section>
    </main>
  );
}
