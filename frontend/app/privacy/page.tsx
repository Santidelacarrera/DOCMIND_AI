"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type PolicySummary = {
  framework: string;
  organization_id: string;
  llm_provider: string;
  embedding_provider: string;
  storage_provider: string;
  antivirus_provider: string;
  rls_enforced: boolean;
  pii_detectors: string[];
  conversations_encrypted_at_rest: boolean;
  retention: {
    deleted_documents_days: number;
    live_documents_days: number;
    conversations_days: number;
  };
  third_party_ai_transfer_warning: boolean;
  third_party_ai_transfer_note: string;
  arcop_endpoints: {
    access_and_portability: string;
    erasure_history_and_conversations: string;
    erasure_documents: string;
  };
};

type ExportBundle = {
  schema_version: string;
  exported_at: string;
  documents?: unknown[];
  query_history?: unknown[];
  conversations?: unknown[];
  audit_events?: unknown[];
};

export default function PrivacyPage() {
  const [signedIn, setSignedIn] = useState(false);
  const [orgId, setOrgId] = useState("");
  const [orgName, setOrgName] = useState("");
  const [policy, setPolicy] = useState<PolicySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [confirmErase, setConfirmErase] = useState(false);
  const [erasing, setErasing] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  const loadWorkspacePolicy = useCallback(async () => {
    setLoading(true);
    try {
      const orgsRes = await api("/api/v1/organizations");
      if (!orgsRes.ok) {
        setSignedIn(false);
        return;
      }
      const orgs = (await orgsRes.json()) as { id: string; name?: string }[];
      setSignedIn(true);
      const first = orgs[0];
      if (!first) return;
      setOrgId(first.id);
      setOrgName(first.name ?? "");
      const polRes = await api(
        `/api/v1/privacy/policy-summary?organization_id=${encodeURIComponent(first.id)}`,
      );
      if (polRes.ok) {
        setPolicy((await polRes.json()) as PolicySummary);
      }
    } catch {
      setSignedIn(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(loadWorkspacePolicy);
  }, [loadWorkspacePolicy]);

  async function handleExport() {
    if (!orgId) return;
    setExporting(true);
    setErrorMessage("");
    setStatusMessage("");
    try {
      const res = await api(
        `/api/v1/privacy/me/export?organization_id=${encodeURIComponent(orgId)}`,
      );
      if (!res.ok) {
        setErrorMessage("Could not export your data. Please sign in again and retry.");
        return;
      }
      const bundle = (await res.json()) as ExportBundle;
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().slice(0, 10);
      const link = document.createElement("a");
      link.href = url;
      link.download = `docmind-privacy-export-${stamp}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setStatusMessage(
        `Exported JSON (${bundle.schema_version}): ${bundle.documents?.length ?? 0} documents, ` +
          `${bundle.query_history?.length ?? 0} queries, ${bundle.conversations?.length ?? 0} conversations, ` +
          `${bundle.audit_events?.length ?? 0} audit events.`,
      );
    } catch {
      setErrorMessage("Network error while exporting personal data.");
    } finally {
      setExporting(false);
    }
  }

  async function handleEraseHistory() {
    if (!orgId) return;
    setErasing(true);
    setErrorMessage("");
    setStatusMessage("");
    try {
      const res = await api(
        `/api/v1/privacy/me/history?organization_id=${encodeURIComponent(orgId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        setErrorMessage("Could not erase your history. Please sign in again and retry.");
        return;
      }
      const result = (await res.json()) as {
        queries_deleted: number;
        conversations_deleted: number;
      };
      setConfirmErase(false);
      setStatusMessage(
        `Erased ${result.queries_deleted} query history entries and ${result.conversations_deleted} conversations.`,
      );
    } catch {
      setErrorMessage("Network error while erasing personal history.");
    } finally {
      setErasing(false);
    }
  }

  return (
    <main>
      {signedIn ? (
        <Nav current="privacy" />
      ) : (
        <nav aria-label="Public">
          <Link href="/">DocMind AI</Link>
          <Link href="/login">Sign in</Link>
          <Link href="/register">Create workspace</Link>
        </nav>
      )}

      <section className="panel wide-panel">
        <p className="eyebrow">DATA PROTECTION · LEY N° 21.719 (CHILE)</p>
        <h1>Privacy &amp; ARCOP Rights</h1>
        <p>
          DocMind AI implements privacy-by-design controls aligned with Chile&apos;s Personal Data
          Protection Law (Ley N° 21.719, effective December 1, 2026). For B2B workspaces, DocMind
          AI acts as <strong>Data Processor (Encargado de Tratamiento)</strong> for uploaded
          documents on behalf of your organization, and as{" "}
          <strong>Data Controller (Responsable)</strong> for account authentication and billing
          records.
        </p>

        {signedIn && policy ? (
          <div style={{ marginTop: 24 }}>
            <h2>
              Workspace Privacy Posture{orgName ? ` — ${orgName}` : ""}
            </h2>
            {policy.third_party_ai_transfer_warning && (
              <div className="field" role="alert" style={{ marginTop: 12, borderRadius: 10 }}>
                <div>
                  <span className="badge warn">AI Transfer Notice</span>
                </div>
                <p style={{ margin: 0 }}>{policy.third_party_ai_transfer_note}</p>
              </div>
            )}

            <div className="table-wrap" style={{ marginTop: 16 }}>
              <table>
                <tbody>
                  <tr>
                    <th>Regulatory framework</th>
                    <td>{policy.framework}</td>
                  </tr>
                  <tr>
                    <th>LLM provider</th>
                    <td>
                      <span
                        className={
                          policy.third_party_ai_transfer_warning ? "badge warn" : "badge ok"
                        }
                      >
                        {policy.llm_provider}
                      </span>
                    </td>
                  </tr>
                  <tr>
                    <th>Vector embeddings</th>
                    <td>
                      <span className={policy.embedding_provider === "local" ? "badge ok" : "badge"}>
                        {policy.embedding_provider}
                      </span>{" "}
                      {policy.embedding_provider === "local"
                        ? "(runs locally — no third-party embedding transfer)"
                        : ""}
                    </td>
                  </tr>
                  <tr>
                    <th>PII masking before indexing</th>
                    <td>
                      <div className="chip-list">
                        {policy.pii_detectors.map((det) => (
                          <span key={det} className="badge ok">
                            {det}
                          </span>
                        ))}
                      </div>
                    </td>
                  </tr>
                  <tr>
                    <th>Conversations encrypted at rest (AES-GCM)</th>
                    <td>
                      <span
                        className={
                          policy.conversations_encrypted_at_rest ? "badge ok" : "badge warn"
                        }
                      >
                        {policy.conversations_encrypted_at_rest ? "Enabled" : "Disabled"}
                      </span>
                    </td>
                  </tr>
                  <tr>
                    <th>PostgreSQL Row-Level Security (RLS)</th>
                    <td>
                      <span className={policy.rls_enforced ? "badge ok" : "badge warn"}>
                        {policy.rls_enforced ? "Enforced" : "App-level isolation"}
                      </span>
                    </td>
                  </tr>
                  <tr>
                    <th>Storage &amp; Antivirus</th>
                    <td>
                      {policy.storage_provider} / {policy.antivirus_provider}
                    </td>
                  </tr>
                  <tr>
                    <th>Retention schedule</th>
                    <td>
                      Deleted documents purge: {policy.retention.deleted_documents_days}d · Live
                      documents:{" "}
                      {policy.retention.live_documents_days > 0
                        ? `${policy.retention.live_documents_days}d`
                        : "Organization-controlled"}{" "}
                      · Conversations: {policy.retention.conversations_days}d
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div style={{ marginTop: 28 }}>
              <h2>Self-Service ARCOP Controls</h2>
              <p>
                Exercise your rights of <strong>Access &amp; Portability</strong> (Arts. 5 &amp; 9)
                and <strong>Cancellation / Erasure</strong> (Art. 7) directly for your account in
                this workspace.
              </p>

              <div className="toolbar">
                <button type="button" onClick={handleExport} disabled={exporting}>
                  {exporting ? "Preparing export…" : "Download my data (JSON)"}
                </button>
                {!confirmErase ? (
                  <button
                    type="button"
                    className="linkbutton"
                    onClick={() => {
                      setConfirmErase(true);
                      setStatusMessage("");
                      setErrorMessage("");
                    }}
                  >
                    Erase my query &amp; conversation history
                  </button>
                ) : (
                  <div className="toolbar" style={{ marginTop: 0 }}>
                    <button
                      type="button"
                      onClick={handleEraseHistory}
                      disabled={erasing}
                      style={{ background: "#ef4444", color: "#fff" }}
                    >
                      {erasing ? "Erasing…" : "Confirm permanent erasure"}
                    </button>
                    <button
                      type="button"
                      className="linkbutton"
                      onClick={() => setConfirmErase(false)}
                      disabled={erasing}
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </div>

              {statusMessage && (
                <p role="status" style={{ marginTop: 14, color: "#6ee7b7" }}>
                  {statusMessage}
                </p>
              )}
              {errorMessage && (
                <p className="error" role="alert" style={{ marginTop: 14 }}>
                  {errorMessage}
                </p>
              )}
            </div>
          </div>
        ) : !loading ? (
          <div style={{ marginTop: 20 }}>
            <p>
              Sign in to inspect your workspace&apos;s live privacy posture, export your personal
              data in portable JSON, or erase your query and conversation history.
            </p>
            <Link href="/login?next=/privacy" className="button">
              Sign in to manage privacy rights
            </Link>
          </div>
        ) : null}
      </section>

      <section className="panel wide-panel">
        <h2>Privacy Notice Summary (Ley N° 21.719)</h2>
        <ul>
          <li>
            <strong>Purpose &amp; Legal Basis (Arts. 3 &amp; 12)</strong>
            <span>
              Documents and extracted fields are processed exclusively to provide document
              extraction, semantic search, and workspace auditability under your organization&apos;s
              contract.
            </span>
          </li>
          <li>
            <strong>Data Minimization &amp; PII Protection (Art. 14 quáter)</strong>
            <span>
              Deterministic PII redaction (including Chilean RUT mod-11, emails, phones, IBAN, and
              card numbers) runs before vector indexing. Conversations are encrypted per
              organization with AES-GCM.
            </span>
          </li>
          <li>
            <strong>International Transfers &amp; Subprocessors (Arts. 27–28)</strong>
            <span>
              Vector embeddings run locally inside the platform. Enterprise deployments use Google
              Cloud Vertex AI under a signed Data Processing Agreement (Zero Data Retention) and
              isolated cloud infrastructure.
            </span>
          </li>
          <li>
            <strong>ARCOP Rights (Arts. 5–9)</strong>
            <span>
              Access &amp; Portability via JSON export, Rectification via human field review,
              Erasure via immediate history/document deletion, and Opposition via your workspace
              administrator. Statutory response deadline: 30 calendar days.
            </span>
          </li>
          <li>
            <strong>Auditability &amp; Security Incidents (Art. 14 quinquies)</strong>
            <span>
              Authentication, document access, deletions, and privacy requests are logged in
              tamper-evident workspace audit trails without storing raw document contents.
            </span>
          </li>
        </ul>
      </section>
    </main>
  );
}
