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
  return (
    <main>
      <Nav current="schemas" />
      <section className="panel">
        <h1>Extraction schemas</h1>
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
          <p key={x.id}>
            <strong>{x.name}</strong> — {x.active ? "Active" : "Inactive"}
          </p>
        ))}
        {!items.length && <p>No schemas yet.</p>}
      </section>
    </main>
  );
}
