"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { API_URL, api } from "../../../lib/api";
type Field = {
  id: string;
  name: string;
  value: unknown;
  confidence: number | null;
  manually_verified: boolean;
};
export default function DocumentDetail() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [fields, setFields] = useState<Field[]>([]);
  const [message, setMessage] = useState("Loading document…");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
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
        setFields((await data.json()).fields);
        setMessage("");
      } else {
        setMessage(
          "Extraction is not ready yet. Keep this page open and retry after processing completes.",
        );
        timer = setTimeout(load, 1000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [params.id, router]);
  async function save(field: Field) {
    if (!org) return;
    let value: unknown = draft;
    try {
      value = JSON.parse(draft);
    } catch {}
    const res = await api(
      `/api/v1/extraction-fields/${field.id}?organization_id=${org}`,
      { method: "PATCH", body: JSON.stringify(value) },
    );
    if (res.ok) {
      setFields(
        fields.map((x) =>
          x.id === field.id ? { ...x, value, manually_verified: true } : x,
        ),
      );
      setEditing(null);
    } else setMessage("Could not save the field.");
  }
  function download(format: string) {
    if (org)
      window.open(
        `${API_URL}/api/v1/documents/${params.id}/export?organization_id=${org}&format=${format}`,
        "_blank",
      );
  }
  return (
    <main>
      <nav>
        <Link href="/dashboard">DocMind AI</Link>
        <Link href="/documents">Documents</Link>
      </nav>
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
              src={`${API_URL}/api/v1/documents/${params.id}/download?organization_id=${org}`}
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
          {message && <p>{message}</p>}
          {fields.map((field) => (
            <article className="field" key={field.id}>
              <label>{field.name}</label>
              {editing === field.id ? (
                <>
                  <input
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
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
                    {field.manually_verified ? " · Verified manually" : ""}
                  </small>
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
          ))}
        </section>
      </div>
    </main>
  );
}
