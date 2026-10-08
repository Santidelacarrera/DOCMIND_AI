"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import Nav from "../../components/Nav";
import { API_URL, api, csrfHeaders } from "../../lib/api";

type Organization = { id: string; name: string };
type Project = { id: string; name: string };
type Document = { id: string; filename: string; status: string | null };
type Schema = { id: string; name: string; active: boolean };

const UPLOAD_ERRORS: Record<string, string> = {
  FILE_TOO_LARGE: "The file is too large.",
  UNSUPPORTED_FILE_TYPE: "Only PDF files are supported.",
  DOCUMENT_INVALID: "The PDF could not be read.",
  PDF_PAGE_LIMIT_EXCEEDED: "The PDF has too many pages.",
  DUPLICATE_DOCUMENT: "This document was already uploaded to the project.",
  QUOTA_EXCEEDED: "Your plan's page quota is exhausted.",
  MALWARE_DETECTED: "The file was rejected by the malware scanner.",
  RATE_LIMITED: "Too many uploads. Try again in a minute.",
};

async function uploadError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return UPLOAD_ERRORS[body.detail] ?? "Please try again.";
  } catch {}
  return "Please try again.";
}

export default function Documents() {
  const [organizationId, setOrganizationId] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [schemas, setSchemas] = useState<Schema[]>([]);
  const [schemaId, setSchemaId] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");
  const [projectName, setProjectName] = useState("");
  const [uploading, setUploading] = useState(false);

  async function load(orgId: string) {
    const [projectResponse, documentResponse, schemaResponse] = await Promise.all([
      api(`/api/v1/projects?organization_id=${orgId}`),
      api(`/api/v1/documents?organization_id=${orgId}`),
      api(`/api/v1/schemas?organization_id=${orgId}`),
    ]);
    if (projectResponse.ok) {
      const values = (await projectResponse.json()) as Project[];
      setProjects(values);
      setProjectId(values[0]?.id ?? "");
    }
    if (documentResponse.ok) setDocuments((await documentResponse.json()) as Document[]);
    if (schemaResponse.ok) setSchemas((await schemaResponse.json()) as Schema[]);
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

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!organizationId || !projectName.trim()) return;
    const response = await api(
      `/api/v1/projects?organization_id=${encodeURIComponent(organizationId)}&name=${encodeURIComponent(projectName.trim())}`,
      { method: "POST" },
    );
    if (response.ok) {
      setProjectName("");
      setMessage("Project created.");
      await load(organizationId);
    } else setMessage("Could not create the project.");
  }

  async function remove(id: string) {
    if (!window.confirm("Delete this document? The stored PDF is removed permanently.")) return;
    const response = await api(
      `/api/v1/documents/${id}?organization_id=${encodeURIComponent(organizationId)}`,
      { method: "DELETE" },
    );
    if (response.ok) {
      setMessage("Document deleted.");
      await load(organizationId);
    } else setMessage("Could not delete the document.");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || !organizationId || !projectId) return;
    setUploading(true);
    const data = new FormData();
    data.append("file", file);
    const schemaParam = schemaId ? `&schema_id=${encodeURIComponent(schemaId)}` : "";
    const response = await fetch(
      `${API_URL}/api/v1/documents?organization_id=${encodeURIComponent(organizationId)}&project_id=${encodeURIComponent(projectId)}${schemaParam}`,
      { method: "POST", body: data, credentials: "include", headers: csrfHeaders() },
    );
    setUploading(false);
    if (response.ok) {
      setMessage("Document queued for processing.");
      await load(organizationId);
    } else setMessage(`Upload failed: ${await uploadError(response)}`);
  }

  return (
    <main>
      <Nav current="documents" />
      <section className="panel">
        <h1>Upload a PDF</h1>
        <form onSubmit={createProject}>
          <label>New project
            <input value={projectName} maxLength={160} onChange={(event) => setProjectName(event.target.value)} placeholder="Invoices 2026" />
          </label>
          <button disabled={!projectName.trim()}>Create project</button>
        </form>
        {!projects.length && <p>Create a project before uploading.</p>}
        <form onSubmit={submit}>
          <label>Project
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)} required>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <label>Extraction schema
            <select value={schemaId} onChange={(event) => setSchemaId(event.target.value)}>
              <option value="">Use the project/org default</option>
              {schemas.filter((s) => s.active).map((schema) => (
                <option key={schema.id} value={schema.id}>{schema.name}</option>
              ))}
            </select>
          </label>
          <label>PDF
            <input type="file" accept="application/pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required />
          </label>
          <button disabled={!projectId || uploading}>{uploading ? "Uploading…" : "Upload and process"}</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>
      <section className="panel">
        <h2>Documents</h2>
        <ul>
          {documents.map((document) => (
            <li key={document.id}>
              <Link href={`/documents/${document.id}`}>{document.filename}</Link>
              <small> {document.status ?? ""}</small>
              <button type="button" className="linkbutton" onClick={() => remove(document.id)} aria-label={`Delete ${document.filename}`}>Delete</button>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
