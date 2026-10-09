"use client";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Nav from "../../../components/Nav";
import { API_URL, api } from "../../../lib/api";
type Field = {
  id: string;
  name: string;
  value: unknown;
  original_value: unknown;
  confidence: number | null;
  manually_verified: boolean;
  needs_review: boolean;
  correction_reason: string | null;
};
type ValidationIssue = { field: string | null; rule: string; message: string };
type Review = { status: "not_required" | "pending" | "approved"; pending_fields: string[] };
// Why a person changed a value; stored with the correction and shown in exports.
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
      setOrg(list[0].id);
      const data = await api(
        `/api/v1/documents/${params.id}/extraction?organization_id=${list[0].id}`,
      );
      if (data.ok) {
        if (cancelled) return;
        const payload = await data.json();
        setFields(payload.fields);
        setRequiresReview(Boolean(payload.requires_review));
        setValidationIssues(payload.validation_issues ?? []);
        setReview(payload.review ?? { status: "not_required", pending_fields: [] });
        setMessage("");
      } else {
        const status = await api(
          `/api/v1/documents/${params.id}/status?organization_id=${list[0].id}`,
        );
        if (cancelled) return;
        if (!status.ok) {
          setMessage("Document not found or not available.");
          return;
        }
        const info = (await status.json()) as { status: string };
        if (info?.status === "FAILED") {
          setFailed(true);
          setMessage("Processing failed. You can retry it.");
          return;
        }
        setFailed(false);
        setMessage(
          `Processing (${info.status.toLowerCase().replace("_", " ")})… this page updates automatically.`,
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

  function download(format: string) {
    if (org)
      window.open(
        `${API_URL}/api/v1/documents/${params.id}/export?organization_id=${org}&format=${format}`,
        "_blank",
        "noopener,noreferrer",
      );
  }
  return (
    <main>
      <Nav current="documents" />
      <div className="detail">
        <section className="viewer">
          <div className="viewerbar">
            <span>Original PDF</span>
            {org && (
              <a
                href={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}`}
              >
                Download
              </a>
            )}
          </div>
          {org ? (
            <iframe
              title="PDF document"
              src={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}&inline=true`}
            />
          ) : (
            <p>{message}</p>
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
          {message && <p role="status">{message}</p>}
          {failed && <button onClick={retry}>Retry processing</button>}
          {review.status === "approved" && <p role="status">Reviewed and approved.</p>}
          {requiresReview && review.status !== "approved" && (
            <div role="alert" className="field">
              <strong>Needs review</strong>
              {review.pending_fields.length > 0 && (
                <small>
                  {review.pending_fields.length} field{review.pending_fields.length === 1 ? "" : "s"} still to check
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
          {fields.map((field) => {
            const lowConfidence = field.needs_review;
            const corrected =
              field.manually_verified &&
              JSON.stringify(field.value) !== JSON.stringify(field.original_value);
            return (
            <article className={`field${lowConfidence ? " low-confidence" : ""}`} key={field.id}>
              <label>{field.name}</label>
              {editing === field.id ? (
                <>
                  <input
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
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
                  <small>
                    {field.confidence !== null
                      ? `${Math.round(field.confidence * 100)}% confidence`
                      : "Confidence unavailable"}
                    {lowConfidence ? " · Needs review" : ""}
                    {field.manually_verified ? " · Verified manually" : ""}
                  </small>
                  {corrected && (
                    <small>
                      Model said: {String(field.original_value ?? "—")}
                      {field.correction_reason ? ` · reason: ${field.correction_reason.replace("_", " ")}` : ""}
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
        </section>
      </div>
    </main>
  );
}
