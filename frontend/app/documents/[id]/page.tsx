"use client";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Nav from "../../../components/Nav";
import { API_URL, api, csrfHeaders } from "../../../lib/api";

type Trust = "human_reviewed" | "automatic_evidenced" | "automatic_unverified";
type Evidence = {
  status: "exact" | "normalized" | "not_found" | "not_applicable" | "unavailable";
  document_id: string;
  page: number | null;
  snippet: string | null;
};
type Field = {
  id: string;
  name: string;
  value: unknown;
  original_value: unknown;
  confidence: number | null;
  manually_verified: boolean;
  needs_review: boolean;
  correction_reason: string | null;
  trust: Trust;
  evidence: Evidence;
};
type PreviewPage = {
  page_number: number;
  text: string;
  ocr_used: boolean;
  ocr_confidence: number | null;
};
type EntityEntry = { value: string; page: number; snippet: string };
type SummaryResult = {
  summary: string;
  abstained: boolean;
  bullet_points: { page: number; text: string }[];
};

const TRUST_TEXT: Record<Trust, string> = {
  human_reviewed: "Confirmed or corrected by a person",
  automatic_evidenced: "Found in the document text",
  automatic_unverified: "Not verified automatically",
};

function evidenceNote(evidence: Evidence): string {
  if (evidence.status === "not_found") {
    return "The value could not be found in the document text. Check it against the original.";
  }
  if (evidence.status === "exact" || evidence.status === "normalized") {
    const where = evidence.page !== null ? `page ${evidence.page}` : "the document";
    const how = evidence.status === "normalized" ? " (same number or date, different notation)" : "";
    return `Source: ${where}${how}`;
  }
  return "No source location is available for this value.";
}

type ValidationIssue = { field: string | null; rule: string; message: string };
type Review = { status: "not_required" | "pending" | "approved"; pending_fields: string[] };
const REASONS: [string, string][] = [
  ["wrong_value", "Model value was wrong"],
  ["missing_value", "Model missed the value"],
  ["ocr_misread", "Scan was misread (OCR)"],
  ["hallucinated_value", "Value is not in the document"],
  ["format", "Formatting / normalisation"],
  ["other", "Other"],
];

export default function DocumentDetail() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [fields, setFields] = useState<Field[]>([]);
  const [requiresReview, setRequiresReview] = useState(false);
  const [validationIssues, setValidationIssues] = useState<ValidationIssue[]>([]);
  const [review, setReview] = useState<Review>({ status: "not_required", pending_fields: [] });
  const [reason, setReason] = useState("wrong_value");
  const [message, setMessage] = useState("Loading document…");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [failed, setFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [filename, setFilename] = useState("");
  const [mimeType, setMimeType] = useState("");
  const [category, setCategory] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [pagesPreview, setPagesPreview] = useState<PreviewPage[]>([]);
  const [tab, setTab] = useState<"fields" | "entities" | "summary">("fields");
  const [entities, setEntities] = useState<Record<string, EntityEntry[]> | null>(null);
  const [summary, setSummary] = useState<SummaryResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      const orgs = await api("/api/v1/organizations");
      if (!orgs.ok) {
        router.replace("/login");
        return;
      }
      const list = (await orgs.json()) as { id: string }[];
      if (!list[0]) {
        setMessage("No workspace is available.");
        return;
      }
      const orgId = list[0].id;
      setOrg(orgId);
      const info = await api(`/api/v1/documents/${params.id}?organization_id=${orgId}`);
      if (info.ok && !cancelled) {
        const meta = (await info.json()) as {
          filename?: string;
          mime_type?: string;
          category?: string | null;
          tags?: string[];
        };
        setFilename(meta.filename ?? "");
        setMimeType(meta.mime_type ?? "");
        setCategory(meta.category ?? "");
        setTagsText((meta.tags ?? []).join(", "));
      }
      const data = await api(
        `/api/v1/documents/${params.id}/extraction?organization_id=${orgId}`,
      );
      if (data.ok) {
        if (cancelled) return;
        const payload = await data.json();
        setFields(payload.fields);
        setRequiresReview(Boolean(payload.requires_review));
        setValidationIssues(payload.validation_issues ?? []);
        setReview(payload.review ?? { status: "not_required", pending_fields: [] });
        setMessage("");
        const prev = await api(`/api/v1/documents/${params.id}/preview?organization_id=${orgId}`);
        if (prev.ok && !cancelled) {
          setPagesPreview(((await prev.json()) as { pages: PreviewPage[] }).pages);
        }
      } else {
        const status = await api(
          `/api/v1/documents/${params.id}/status?organization_id=${orgId}`,
        );
        if (cancelled) return;
        if (!status.ok) {
          setMessage("Document not found or not available.");
          return;
        }
        const st = (await status.json()) as { status: string };
        if (st?.status === "FAILED") {
          setFailed(true);
          setMessage("Processing failed. You can retry it.");
          return;
        }
        setFailed(false);
        setMessage(
          `Processing (${st.status.toLowerCase().replace("_", " ")})… this page updates automatically.`,
        );
        timer = setTimeout(load, 1500);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [params.id, router, reloadKey]);

  async function save(field: Field) {
    if (!org) return;
    let value: unknown = draft;
    try {
      value = JSON.parse(draft);
    } catch {}
    const changed = JSON.stringify(value) !== JSON.stringify(field.original_value);
    const why = changed ? reason : "confirmed";
    const res = await api(
      `/api/v1/extraction-fields/${field.id}?organization_id=${org}&reason=${encodeURIComponent(why)}`,
      { method: "PATCH", body: JSON.stringify(value) },
    );
    if (res.ok) {
      setFields(
        fields.map((x) =>
          x.id === field.id
            ? { ...x, value, manually_verified: true, needs_review: false, correction_reason: why }
            : x,
        ),
      );
      setReview((r) => ({ ...r, pending_fields: r.pending_fields.filter((n) => n !== field.name) }));
      setEditing(null);
    } else setMessage("Could not save the field.");
  }

  async function approve() {
    if (!org) return;
    const res = await api(
      `/api/v1/documents/${params.id}/review/approve?organization_id=${org}`,
      { method: "POST" },
    );
    if (res.ok) {
      setReview((r) => ({ ...r, status: "approved", pending_fields: [] }));
      setMessage("");
    } else if (res.status === 409) {
      const body = (await res.json()) as { pending_fields?: string[] };
      setMessage(`${(body.pending_fields ?? []).length} flagged field(s) still need a decision before approval.`);
    } else setMessage("Could not approve the review.");
  }

  async function retry() {
    if (!org) return;
    const res = await api(
      `/api/v1/documents/${params.id}/process?organization_id=${org}`,
      { method: "POST" },
    );
    if (res.ok) {
      setFailed(false);
      setMessage("Processing restarted…");
      setReloadKey((n) => n + 1);
    } else setMessage("Could not restart processing.");
  }

  async function uploadVersion(file: File | undefined) {
    if (!org || !file) return;
    const data = new FormData();
    data.append("file", file);
    const res = await fetch(`${API_URL}/api/v1/documents/${params.id}/versions?organization_id=${org}`, {
      method: "POST",
      body: data,
      credentials: "include",
      headers: csrfHeaders(),
    });
    if (res.status === 409) setMessage("The document is still processing, or this file is already another document.");
    else if (!res.ok) setMessage("The new version could not be uploaded.");
    else if ((await res.json()).status === "unchanged") setMessage("That file is the current version already.");
    else {
      setMessage("New version queued: it replaces the content, extractions and search index of this document.");
      setReloadKey((n) => n + 1);
    }
  }

  async function saveMetadata() {
    if (!org) return;
    const tags = tagsText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const res = await api(`/api/v1/documents/${params.id}/metadata?organization_id=${org}`, {
      method: "PATCH",
      body: JSON.stringify({ category: category || null, tags }),
    });
    if (res.ok) setMessage("Metadata updated.");
    else setMessage("Could not update metadata.");
  }

  async function loadEntities() {
    setTab("entities");
    if (!org || entities) return;
    const res = await api(`/api/v1/documents/${params.id}/entities?organization_id=${org}`);
    if (res.ok) setEntities(((await res.json()) as { entities: Record<string, EntityEntry[]> }).entities);
  }

  async function runSummary() {
    setTab("summary");
    if (!org || summary) return;
    const res = await api(`/api/v1/documents/${params.id}/summarize?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ max_sentences: 5 }),
    });
    if (res.ok) setSummary((await res.json()) as SummaryResult);
  }

  function download(format: string) {
    if (org)
      window.open(
        `${API_URL}/api/v1/documents/${params.id}/export?organization_id=${org}&format=${format}`,
        "_blank",
        "noopener,noreferrer",
      );
  }

  const isPdf = mimeType === "application/pdf" || /\.pdf$/i.test(filename);
  const isImage = /^image\/(png|jpeg|webp)$/i.test(mimeType) || /\.(png|jpe?g|webp)$/i.test(filename);

  return (
    <main>
      <Nav current="documents" />
      <div className="detail">
        <section className="viewer">
          <div className="viewerbar">
            <span>{filename || "Document"}</span>
            {org && (
              <a href={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}`}>
                Download
              </a>
            )}
          </div>
          {org && isPdf ? (
            <iframe
              title="PDF document"
              src={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}&inline=true`}
            />
          ) : org && isImage ? (
            <div>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                className="preview-img"
                alt={filename}
                src={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}&inline=true`}
              />
              {pagesPreview.length > 0 && (
                <div className="page-preview">
                  <strong>OCR text</strong>
                  {pagesPreview.map((p) => (
                    <pre key={p.page_number}>{p.text || "(no text recognized)"}</pre>
                  ))}
                </div>
              )}
            </div>
          ) : pagesPreview.length > 0 ? (
            <div className="page-preview">
              {pagesPreview.map((p) => (
                <div key={p.page_number}>
                  <small>
                    Page / Section {p.page_number}
                    {p.ocr_used && p.ocr_confidence !== null
                      ? ` · OCR ${Math.round(p.ocr_confidence * 100)}%`
                      : ""}
                  </small>
                  <pre>{p.text}</pre>
                </div>
              ))}
            </div>
          ) : (
            <p className="panel">Download the original to inspect this file.</p>
          )}
        </section>

        <section className="results">
          <div className="viewerbar">
            <strong>Extracted data</strong>
            <span>
              <button onClick={() => download("json")}>JSON</button>
              <button onClick={() => download("csv")}>CSV</button>
              <button onClick={() => download("xlsx")}>XLSX</button>
            </span>
          </div>
          <div className="tabs">
            <button
              type="button"
              className={tab === "fields" ? "active" : ""}
              onClick={() => setTab("fields")}
            >
              Fields
            </button>
            <button
              type="button"
              className={tab === "entities" ? "active" : ""}
              onClick={() => void loadEntities()}
            >
              Entities
            </button>
            <button
              type="button"
              className={tab === "summary" ? "active" : ""}
              onClick={() => void runSummary()}
            >
              Summary
            </button>
          </div>
          {message && <p role="status">{message}</p>}
          {failed && <button onClick={retry}>Retry processing</button>}

          {tab === "fields" && (
            <>
              <div className="field">
                <label>
                  Category &amp; tags
                  <input
                    value={category}
                    placeholder="contract, invoice…"
                    onChange={(e) => setCategory(e.target.value)}
                  />
                  <input
                    value={tagsText}
                    placeholder="tag1, tag2"
                    onChange={(e) => setTagsText(e.target.value)}
                  />
                </label>
                <button type="button" onClick={() => void saveMetadata()}>
                  Save tags
                </button>
              </div>
              <label className="field">
                Upload a new version
                <input
                  type="file"
                  accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.png,.jpg,.jpeg,.webp,.tif,.tiff"
                  onChange={(e) => void uploadVersion(e.target.files?.[0])}
                />
              </label>
              {review.status === "approved" && <p role="status">Reviewed and approved.</p>}
              {requiresReview && review.status !== "approved" && (
                <div role="alert" className="field">
                  <strong>Needs review</strong>
                  {review.pending_fields.length > 0 && (
                    <small>
                      {review.pending_fields.length} field
                      {review.pending_fields.length === 1 ? "" : "s"} still to check
                    </small>
                  )}
                  <button onClick={approve} disabled={review.pending_fields.length > 0}>
                    Approve review
                  </button>
                  <ul>
                    {validationIssues.map((issue, index) => (
                      <li key={index}>
                        {issue.field ? `${issue.field}: ` : ""}
                        {issue.message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {fields.length > 0 && (
                <p>
                  <small>
                    Confidence values are scores reported by the extraction model. They are not
                    calibrated probabilities: use the source location, not the percentage, to decide
                    whether a value is right.
                  </small>
                </p>
              )}
              {fields.map((field) => {
                const lowConfidence = field.needs_review;
                const corrected =
                  field.manually_verified &&
                  JSON.stringify(field.value) !== JSON.stringify(field.original_value);
                return (
                  <article
                    className={`field${lowConfidence ? " low-confidence" : ""}`}
                    key={field.id}
                  >
                    <label>{field.name}</label>
                    {editing === field.id ? (
                      <>
                        <input value={draft} onChange={(e) => setDraft(e.target.value)} />
                        <select
                          aria-label="Reason for the change"
                          value={reason}
                          onChange={(e) => setReason(e.target.value)}
                        >
                          {REASONS.map(([code, label]) => (
                            <option key={code} value={code}>
                              {label}
                            </option>
                          ))}
                        </select>
                        <button onClick={() => save(field)}>Save</button>
                        <button onClick={() => setEditing(null)}>Cancel</button>
                      </>
                    ) : (
                      <>
                        <strong>{String(field.value ?? "—")}</strong>
                        <small>{TRUST_TEXT[field.trust] ?? "Verification status unknown"}</small>
                        <small>{evidenceNote(field.evidence)}</small>
                        {field.evidence.snippet && (
                          <blockquote>
                            <small>{field.evidence.snippet}</small>
                          </blockquote>
                        )}
                        {field.evidence.status === "not_found" && field.value !== null && (
                          <small role="status">Not found in the document text</small>
                        )}
                        <small>
                          {field.confidence !== null
                            ? `Model score ${Math.round(field.confidence * 100)}% (not calibrated)`
                            : "No model score"}
                          {lowConfidence ? " · Needs review" : ""}
                          {field.manually_verified ? " · Verified manually" : ""}
                        </small>
                        {corrected && (
                          <small>
                            Model said: {String(field.original_value ?? "—")}
                            {field.correction_reason
                              ? ` · reason: ${field.correction_reason.replace("_", " ")}`
                              : ""}
                          </small>
                        )}
                        <button
                          onClick={() => {
                            setEditing(field.id);
                            setDraft(
                              typeof field.value === "string"
                                ? field.value
                                : JSON.stringify(field.value),
                            );
                          }}
                        >
                          Edit
                        </button>
                      </>
                    )}
                  </article>
                );
              })}
            </>
          )}

          {tab === "entities" && (
            <div className="field">
              {!entities ? (
                <p>Loading entities…</p>
              ) : (
                Object.entries(entities).map(([kind, list]) => (
                  <div key={kind}>
                    <strong>{kind}</strong>
                    {list.length ? (
                      <ul>
                        {list.map((e, i) => (
                          <li key={i}>
                            <span>{e.value}</span>
                            <small>p.{e.page}</small>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p>None detected.</p>
                    )}
                  </div>
                ))
              )}
            </div>
          )}

          {tab === "summary" && (
            <div className="field">
              {!summary ? (
                <p>Generating grounded summary…</p>
              ) : (
                <>
                  <p>{summary.summary}</p>
                  {summary.bullet_points.map((b, i) => (
                    <blockquote key={i}>
                      <small>p.{b.page}: </small>
                      {b.text}
                    </blockquote>
                  ))}
                </>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
