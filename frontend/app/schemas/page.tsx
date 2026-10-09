"use client";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";
type Schema = {
  id: string;
  name: string;
  description: string | null;
  active: boolean;
};
type Version = {
  id: string;
  version: number;
  json_schema: Record<string, unknown>;
  prompt_instructions: string | null;
  created_at: string;
};
const example = JSON.stringify(
  {
    type: "object",
    properties: {
      invoice_number: { type: "string" },
      total: { type: "number" },
      currency: { type: "string" },
    },
    required: ["invoice_number"],
    additionalProperties: false,
  },
  null,
  2,
);
export default function Schemas() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [items, setItems] = useState<Schema[]>([]);
  const [name, setName] = useState("");
  const [schema, setSchema] = useState(example);
  const [message, setMessage] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [versions, setVersions] = useState<Record<string, Version[]>>({});
  const [newVersionSchema, setNewVersionSchema] = useState(example);
  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (!first) return;
    setOrg(first.id);
    const res = await api(`/api/v1/schemas?organization_id=${first.id}`);
    if (res.ok) setItems(await res.json());
  }, [router]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);
  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      const json_schema = JSON.parse(schema);
      const res = await api(
        `/api/v1/schemas?organization_id=${org}&name=${encodeURIComponent(name)}`,
        { method: "POST", body: JSON.stringify(json_schema) },
      );
      if (!res.ok) throw Error();
      setName("");
      setMessage("Schema created.");
      load();
    } catch {
      setMessage("Enter a valid object JSON Schema.");
    }
  }
  async function toggleVersions(schemaId: string) {
    if (expanded === schemaId) {
      setExpanded(null);
      return;
    }
    setExpanded(schemaId);
    if (!versions[schemaId]) {
      const res = await api(`/api/v1/schemas/${schemaId}/versions?organization_id=${org}`);
      if (res.ok) {
        const data = (await res.json()) as Version[];
        setVersions((v) => ({ ...v, [schemaId]: data }));
      }
    }
  }
  async function addVersion(schemaId: string, e: FormEvent) {
    e.preventDefault();
    try {
      const json_schema = JSON.parse(newVersionSchema);
      const res = await api(`/api/v1/schemas/${schemaId}/versions?organization_id=${org}`, {
        method: "POST",
        body: JSON.stringify(json_schema),
      });
      if (!res.ok) throw Error();
      setMessage("New version published.");
      const refreshed = await api(`/api/v1/schemas/${schemaId}/versions?organization_id=${org}`);
      if (refreshed.ok) {
        const data = (await refreshed.json()) as Version[];
        setVersions((v) => ({ ...v, [schemaId]: data }));
      }
    } catch {
      setMessage("Enter a valid object JSON Schema for the new version.");
    }
  }
  return (
    <main>
      <Nav current="schemas" />
      <section className="panel">
        <h1>Extraction schemas</h1>
        <p>
          Pick one of these when uploading a document to use it instead of the
          project/organization default. Each schema keeps every version it has
          ever had, so in-flight jobs always keep extracting against the
          version they started with.
        </p>
        <form onSubmit={submit}>
          <label>
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </label>
          <label>
            JSON Schema
            <textarea
              value={schema}
              onChange={(e) => setSchema(e.target.value)}
              rows={14}
            />
          </label>
          <button>Create schema</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>
      <section className="panel">
        <h2>Available schemas</h2>
        {items.map((x) => (
          <article key={x.id} className="field">
            <p>
              <strong>{x.name}</strong> — {x.active ? "Active" : "Inactive"}
              <button type="button" className="linkbutton" onClick={() => toggleVersions(x.id)}>
                {expanded === x.id ? "Hide versions" : "View versions"}
              </button>
            </p>
            {expanded === x.id && (
              <div>
                <ul>
                  {(versions[x.id] ?? []).map((v) => (
                    <li key={v.id}>
                      <strong>v{v.version}</strong> — {new Date(v.created_at).toLocaleString()}
                      <details>
                        <summary>JSON Schema</summary>
                        <pre>{JSON.stringify(v.json_schema, null, 2)}</pre>
                      </details>
                    </li>
                  ))}
                  {!(versions[x.id] ?? []).length && <li>Loading versions…</li>}
                </ul>
                <form onSubmit={(e) => addVersion(x.id, e)}>
                  <label>
                    Publish a new version
                    <textarea
                      value={newVersionSchema}
                      onChange={(e) => setNewVersionSchema(e.target.value)}
                      rows={10}
                    />
                  </label>
                  <button>Add version</button>
                </form>
              </div>
            )}
          </article>
        ))}
        {!items.length && <p>No schemas yet.</p>}
      </section>
    </main>
  );
}
