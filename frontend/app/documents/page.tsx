"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import { API_URL, api, csrfHeaders } from "../../lib/api";

type Organization = { id: string; name: string };
type Project = { id: string; name: string };
type Document = { id: string; filename: string };

export default function Documents() {
  const [organizationId, setOrganizationId] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");

  async function load(orgId: string) {
    const [projectResponse, documentResponse] = await Promise.all([
      api(`/api/v1/projects?organization_id=${orgId}`),
      api(`/api/v1/documents?organization_id=${orgId}`),
    ]);
    if (projectResponse.ok) {
      const values = (await projectResponse.json()) as Project[];
      setProjects(values);
      setProjectId(values[0]?.id ?? "");
    }
    if (documentResponse.ok) setDocuments((await documentResponse.json()) as Document[]);
  }

  useEffect(() => {
    void (async () => {
      const response = await api("/api/v1/organizations");
      if (!response.ok) return;
      const organizations = (await response.json()) as Organization[];
      const first = organizations[0];
      if (first) {
        setOrganizationId(first.id);
        await load(first.id);
      }
    })();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || !organizationId || !projectId) return;
    const data = new FormData();
    data.append("file", file);
    const response = await fetch(
      `${API_URL}/api/v1/documents?organization_id=${encodeURIComponent(organizationId)}&project_id=${encodeURIComponent(projectId)}`,
      { method: "POST", body: data, credentials: "include", headers: csrfHeaders() },
    );
    if (response.ok) {
      setMessage("Document queued for processing.");
      await load(organizationId);
    } else setMessage(`Upload failed: ${await response.text()}`);
  }

  return (
    <main>
      <nav><Link href="/dashboard">DocMind AI</Link><strong>Documents</strong></nav>
      <section className="panel">
        <h1>Upload a PDF</h1>
        {!projects.length && <p>Create a project through the API before uploading.</p>}
        <form onSubmit={submit}>
          <label>Project
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)} required>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <label>PDF
            <input type="file" accept="application/pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required />
          </label>
          <button disabled={!projectId}>Upload and process</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>
      <section className="panel">
        <h2>Documents</h2>
        <ul>{documents.map((document) => <li key={document.id}><Link href={`/documents/${document.id}`}>{document.filename}</Link></li>)}</ul>
      </section>
    </main>
  );
}
