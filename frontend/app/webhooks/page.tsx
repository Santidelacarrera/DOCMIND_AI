"use client";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type Webhook = {
  id: string;
  url: string;
  description: string | null;
  events: string[];
  project_ids?: string[] | null;
  active: boolean;
  last_delivery_at: string | null;
  last_delivery_status: number | null;
};
type Project = { id: string; name: string };

const EVENT_OPTIONS = ["document.completed", "document.failed"];

export default function Webhooks() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [items, setItems] = useState<Webhook[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [events, setEvents] = useState<string[]>(["document.completed"]);
  const [message, setMessage] = useState("");
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);

  const load = useCallback(async () => {
    const orgs = await api("/api/v1/organizations");
    if (!orgs.ok) {
      router.replace("/login");
      return;
    }
    const first = ((await orgs.json()) as { id: string }[])[0];
    if (!first) return;
    setOrg(first.id);
    const [res, pRes] = await Promise.all([
      api(`/api/v1/webhooks?organization_id=${first.id}`),
      api(`/api/v1/projects?organization_id=${first.id}`),
    ]);
    if (res.ok) setItems(await res.json());
    if (pRes.ok) setProjects((await pRes.json()) as Project[]);
  }, [router]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  function toggleEvent(event: string) {
    setEvents((current) =>
      current.includes(event) ? current.filter((e) => e !== event) : [...current, event],
    );
  }

  function toggleProject(pid: string) {
    setSelectedProjects((current) =>
      current.includes(pid) ? current.filter((x) => x !== pid) : [...current, pid],
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setRevealedSecret(null);
    const payload: Record<string, unknown> = {
      url,
      description: description || null,
      events,
    };
    if (selectedProjects.length > 0) {
      payload.project_ids = selectedProjects;
    }
    const res = await api(`/api/v1/webhooks?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      const body = await res.json();
      setRevealedSecret(body.signing_secret);
      setUrl("");
      setDescription("");
      setSelectedProjects([]);
      setMessage("Webhook created. Copy the signing secret now — it won't be shown again.");
      await load();
    } else {
      const body = await res.json().catch(() => ({}));
      setMessage(
        body.detail === "WEBHOOK_URL_NOT_ALLOWED" || body.detail === "WEBHOOK_URL_MUST_BE_HTTPS"
          ? "That URL isn't allowed (it must be a public HTTPS endpoint)."
          : "Could not create the webhook.",
      );
    }
  }

  async function remove(id: string) {
    const res = await api(`/api/v1/webhooks/${id}?organization_id=${org}`, { method: "DELETE" });
    if (res.ok) {
      setMessage("Webhook deleted.");
      await load();
    }
  }

  return (
    <main>
      <Nav current="webhooks" />
      <section className="panel">
        <h1>Webhooks</h1>
        <p>
          Get a signed POST request whenever a document finishes processing.
          Verify the <code>X-DocMind-Signature</code> header as{" "}
          <code>sha256=HMAC_SHA256(signing_secret, raw_body)</code>.
        </p>
        <form onSubmit={submit}>
          <label>
            Endpoint URL
            <input
              type="url"
              placeholder="https://your-system.example.com/hooks/docmind"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
            />
          </label>
          <label>
            Description (optional)
            <input value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <fieldset>
            <legend>Events</legend>
            {EVENT_OPTIONS.map((event) => (
              <label key={event}>
                <input
                  type="checkbox"
                  checked={events.includes(event)}
                  onChange={() => toggleEvent(event)}
                />
                {event}
              </label>
            ))}
          </fieldset>
          {projects.length > 0 && (
            <fieldset>
              <legend>Filter by project (optional — leave unchecked for all projects)</legend>
              {projects.map((p) => (
                <label key={p.id}>
                  <input
                    type="checkbox"
                    checked={selectedProjects.includes(p.id)}
                    onChange={() => toggleProject(p.id)}
                  />
                  {p.name}
                </label>
              ))}
            </fieldset>
          )}
          <button disabled={!events.length}>Create webhook</button>
        </form>
        {message && <p role="status">{message}</p>}
        {revealedSecret && (
          <p>
            Signing secret: <code>{revealedSecret}</code>
          </p>
        )}
      </section>
      <section className="panel">
        <h2>Configured webhooks</h2>
        <ul>
          {items.map((w) => (
            <li key={w.id}>
              <strong>{w.url}</strong> — {w.events.join(", ")}
              {w.project_ids && w.project_ids.length > 0
                ? ` · Projects: ${w.project_ids.length}`
                : " · All projects"}
              <br />
              <small>
                {w.last_delivery_at
                  ? `Last delivery ${new Date(w.last_delivery_at).toLocaleString()} (HTTP ${w.last_delivery_status ?? "—"})`
                  : "No deliveries yet"}
              </small>
              <button type="button" className="linkbutton" onClick={() => remove(w.id)}>
                Delete
              </button>
            </li>
          ))}
          {!items.length && <p>No webhooks configured.</p>}
        </ul>
      </section>
    </main>
  );
}
