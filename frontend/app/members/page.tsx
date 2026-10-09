"use client";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

type Member = { user_id: string; email: string; role: string };
type Invitation = {
  id: string;
  email: string;
  role: string;
  status: string;
  expires_at: string;
};

const ROLES = ["VIEWER", "MEMBER", "ADMIN", "OWNER"];

export default function Members() {
  const router = useRouter();
  const [org, setOrg] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("MEMBER");
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
    const [membersRes, invitesRes] = await Promise.all([
      api(`/api/v1/members?organization_id=${first.id}`),
      api(`/api/v1/invitations?organization_id=${first.id}`),
    ]);
    if (membersRes.ok) setMembers(await membersRes.json());
    if (invitesRes.ok) setInvitations(await invitesRes.json());
  }, [router]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function invite(e: FormEvent) {
    e.preventDefault();
    const res = await api(`/api/v1/invitations?organization_id=${org}`, {
      method: "POST",
      body: JSON.stringify({ email, role }),
    });
    if (res.ok) {
      setMessage(`Invitation sent to ${email}.`);
      setEmail("");
      await load();
    } else {
      const body = await res.json().catch(() => ({}));
      setMessage(
        body.detail === "INVITATION_ALREADY_PENDING"
          ? "There is already a pending invitation for that address."
          : body.detail === "ALREADY_MEMBER"
            ? "That person is already a member."
            : "Could not send the invitation.",
      );
    }
  }

  async function revoke(id: string) {
    const res = await api(`/api/v1/invitations/${id}?organization_id=${org}`, { method: "DELETE" });
    if (res.ok) {
      setMessage("Invitation revoked.");
      await load();
    }
  }

  async function changeRole(userId: string, newRole: string) {
    const res = await api(
      `/api/v1/members/${userId}?organization_id=${org}&role=${newRole}`,
      { method: "PATCH" },
    );
    if (res.ok) await load();
    else setMessage("Could not change that member's role.");
  }

  async function removeMember(userId: string) {
    if (!window.confirm("Remove this person from the workspace?")) return;
    const res = await api(`/api/v1/members/${userId}?organization_id=${org}`, { method: "DELETE" });
    if (res.ok) await load();
    else setMessage("Could not remove that member.");
  }

  return (
    <main>
      <Nav current="members" />
      <section className="panel">
        <h1>Workspace members</h1>
        <form onSubmit={invite}>
          <label>
            Email address
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </label>
          <button>Send invitation</button>
        </form>
        {message && <p role="status">{message}</p>}
      </section>
      <section className="panel">
        <h2>Members</h2>
        <ul>
          {members.map((m) => (
            <li key={m.user_id}>
              {m.email}
              <select value={m.role} onChange={(e) => changeRole(m.user_id, e.target.value)}>
                {ROLES.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
              <button type="button" className="linkbutton" onClick={() => removeMember(m.user_id)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="panel">
        <h2>Pending invitations</h2>
        <ul>
          {invitations.filter((i) => i.status === "PENDING").map((i) => (
            <li key={i.id}>
              {i.email} — {i.role} — expires {new Date(i.expires_at).toLocaleDateString()}
              <button type="button" className="linkbutton" onClick={() => revoke(i.id)}>
                Revoke
              </button>
            </li>
          ))}
          {!invitations.filter((i) => i.status === "PENDING").length && <p>No pending invitations.</p>}
        </ul>
      </section>
    </main>
  );
}
