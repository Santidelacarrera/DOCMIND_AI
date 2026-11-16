"use client";

import Link from "next/link";
import { DragEvent, FormEvent, useEffect, useState } from "react";

import Nav from "../../components/Nav";
import { API_URL, api, csrfHeaders } from "../../lib/api";

type Organization = { id: string; name: string };
type Project = { id: string; name: string };
type Document = {
  id: string;
  filename: string;
  status: string | null;
  category?: string | null;
  tags?: string[];
  pages?: number | null;
  project_id?: string;
};
type Schema = { id: string; name: string; active: boolean };

const ACCEPTED_FILES =
  ".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.png,.jpg,.jpeg,.webp,.tif,.tiff,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation,image/png,image/jpeg,image/webp,image/tiff";

const UPLOAD_ERRORS: Record<string, string> = {
  FILE_TOO_LARGE: "The file exceeds the size limit.",
  UNSUPPORTED_FILE_TYPE: "Supported formats: PDF, DOCX, TXT, MD, CSV, XLSX, PPTX, PNG, JPG, WEBP, TIFF.",
  DOCUMENT_INVALID: "The document could not be read or appears corrupted.",
  ACTIVE_CONTENT_REJECTED: "The document contains macros or embedded active content and was rejected.",
  PDF_ENCRYPTED: "Encrypted documents are not supported.",
  DOCUMENT_ENCRYPTED: "Encrypted documents are not supported.",
  PDF_PAGE_LIMIT_EXCEEDED: "The document has too many pages.",
  DUPLICATE_DOCUMENT: "This document was already uploaded to the project.",
  QUOTA_EXCEEDED: "Your plan's monthly quota or storage limit is exhausted.",
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
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState("");
  const [projectName, setProjectName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");

  async function load(orgId: string, q = search, st = statusFilter, cat = categoryFilter) {
    const params = new URLSearchParams({ organization_id: orgId });
    if (q.trim()) params.set("q", q.trim());
    if (st) params.set("status", st);
    if (cat) params.set("category", cat);

    const [projectResult, documentResult, schemaResult] = await Promise.allSettled([
      api(`/api/v1/projects?organization_id=${orgId}`),
      api(`/api/v1/documents?${params.toString()}`),
      api(`/api/v1/schemas?organization_id=${orgId}`),
    ]);
    if (projectResult.status === "fulfilled" && projectResult.value.ok) {
      const values = (await projectResult.value.json()) as Project[];
      setProjects(values);
      setProjectId((prev) => prev || values[0]?.id || "");
    }
    if (documentResult.status === "fulfilled" && documentResult.value.ok) {
      setDocuments((await documentResult.value.json()) as Document[]);
    }
    if (schemaResult.status === "fulfilled" && schemaResult.value.ok) {
      setSchemas((await schemaResult.value.json()) as Schema[]);
    }
  }

  useEffect(() => {
    void (async () => {
      const response = await api("/api/v1/organizations");
      if (!response.ok) return;
      const organizations = (await response.json()) as Organization[];
      const first = organizations[0];
      if (first) {
        setOrganizationId(first.id);
        await load(first.id, "", "", "");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    if (!window.confirm("Delete this document? The stored file is removed permanently.")) return;
    const response = await api(
      `/api/v1/documents/${id}?organization_id=${encodeURIComponent(organizationId)}`,
      { method: "DELETE" },
    );
    if (response.ok) {
      setMessage("Document deleted.");
      await load(organizationId);
    } else setMessage("Could not delete the document.");
  }

  async function retryJob(id: string) {
    const response = await api(
      `/api/v1/documents/${id}/process?organization_id=${encodeURIComponent(organizationId)}`,
      { method: "POST" },
    );
    if (response.ok) {
      setMessage("Reprocessing queued.");
      await load(organizationId);
    } else setMessage("Could not restart processing.");
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
      setFile(null);
      await load(organizationId);
    } else setMessage(`Upload failed: ${await uploadError(response)}`);
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  }

  return (
    <main>
      <Nav current="documents" />
      <section className="panel wide-panel">
        <h1>Upload a document</h1>
        <p>
          Supports PDF, Word (.docx), TXT, Markdown, CSV, Excel (.xlsx), PowerPoint (.pptx) and
          scanned images (PNG, JPG, WEBP, TIFF).
        </p>
        <form onSubmit={createProject}>
          <label>
            New project
            <input
              value={projectName}
              maxLength={160}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="Invoices 2026"
            />
          </label>
          <button disabled={!projectName.trim()}>Create project</button>
        </form>
        {!projects.length && <p>Create a project before uploading.</p>}
        <form onSubmit={submit}>
          <label>
            Project
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)} required>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Extraction schema
            <select value={schemaId} onChange={(event) => setSchemaId(event.target.value)}>
              <option value="">Use the project/org default</option>
              {schemas
                .filter((s) => s.active)
                .map((schema) => (
                  <option key={schema.id} value={schema.id}>
                    {schema.name}
                  </option>
                ))}
            </select>
          </label>
          <div
            className={`dropzone ${dragging ? "active" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <label>
              PDF, Word, Spreadsheet, Slides or Image
              <input
                type="file"
                accept={ACCEPTED_FILES}
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                required={!file}
              />
            </label>
            {file && <small>Selected: {file.name}</small>}
          </div>
          <button disabled={!projectId || !file || uploading}>
            {uploading ? "Uploading…" : "Upload and process"}
          </button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>

      <section className="panel wide-panel">
        <div className="viewerbar">
          <h2>Documents ({documents.length})</h2>
          <Link href="/intelligence">Compare documents</Link>
        </div>
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            if (organizationId) void load(organizationId);
          }}
        >
          <input
            type="search"
            placeholder="Search text, filename or category…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="COMPLETED">COMPLETED</option>
            <option value="QUEUED">QUEUED</option>
            <option value="PROCESSING">PROCESSING</option>
            <option value="FAILED">FAILED</option>
          </select>
          <select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
            <option value="">All categories</option>
            <option value="invoice">Invoice</option>
            <option value="contract">Contract</option>
            <option value="policy">Policy</option>
            <option value="spreadsheet">Spreadsheet</option>
            <option value="presentation">Presentation</option>
            <option value="financial_report">Financial report</option>
            <option value="general">General</option>
          </select>
          <button type="submit">Filter</button>
        </form>
        <ul>
          {documents.map((document) => (
            <li key={document.id}>
              <span>
                <Link href={`/documents/${document.id}`}>{document.filename}</Link>{" "}
                {document.category && <span className="badge">{document.category}</span>}{" "}
                {(document.tags ?? []).slice(0, 3).map((t) => (
                  <span key={t} className="badge">
                    #{t}
                  </span>
                ))}
              </span>
              <span>
                <small>{document.status ?? ""}</small>{" "}
                {document.status === "FAILED" && (
                  <button
                    type="button"
                    className="linkbutton"
                    onClick={() => void retryJob(document.id)}
                  >
                    Retry
                  </button>
                )}{" "}
                <button
                  type="button"
                  className="linkbutton"
                  onClick={() => remove(document.id)}
                  aria-label={`Delete ${document.filename}`}
                >
                  Delete
                </button>
              </span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
