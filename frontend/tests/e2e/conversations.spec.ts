import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

// The same fictitious two-page policy as ask-citations.spec.ts (page 2: "23 dias").
const policy = Buffer.from(
  "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFs1IDAgUiA3IDAgUl0gL0NvdW50IDIgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhIC9FbmNvZGluZyAvV2luQW5zaUVuY29kaW5nID4+CmVuZG9iago0IDAgb2JqCjw8IC9MZW5ndGggMTA5ID4+CnN0cmVhbQpCVAovRjEgMTEgVGYKMTggVEwKNTYgNzQwIFRkCihQT0xJVElDQSBERSBWQUNBQ0lPTkVTKSBUaiBUKgooRG9jdW1lbnRvIGZpY3RpY2lvIHBhcmEgbGEgcHJ1ZWJhIGUyZS4pIFRqIFQqCkVUCmVuZHN0cmVhbQplbmRvYmoKNSAwIG9iago8PCAvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDYxMiA3OTJdIC9SZXNvdXJjZXMgPDwgL0ZvbnQgPDwgL0YxIDMgMCBSID4+ID4+IC9Db250ZW50cyA0IDAgUiA+PgplbmRvYmoKNiAwIG9iago8PCAvTGVuZ3RoIDExMSA+PgpzdHJlYW0KQlQKL0YxIDExIFRmCjE4IFRMCjU2IDc0MCBUZAooTG9zIGVtcGxlYWRvcyB0aWVuZW4gZGVyZWNobyBhIDIzIGRpYXMgbGFib3JhYmxlcyBkZSB2YWNhY2lvbmVzIGFsIGFuby4pIFRqIFQqCkVUCmVuZHN0cmVhbQplbmRvYmoKNyAwIG9iago8PCAvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDYxMiA3OTJdIC9SZXNvdXJjZXMgPDwgL0ZvbnQgPDwgL0YxIDMgMCBSID4+ID4+IC9Db250ZW50cyA2IDAgUiA+PgplbmRvYmoKeHJlZgowIDgKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDE1IDAwMDAwIG4gCjAwMDAwMDAwNjQgMDAwMDAgbiAKMDAwMDAwMDEyNyAwMDAwMCBuIAowMDAwMDAwMjI0IDAwMDAwIG4gCjAwMDAwMDAzODQgMDAwMDAgbiAKMDAwMDAwMDUxMCAwMDAwMCBuIAowMDAwMDAwNjcyIDAwMDAwIG4gCnRyYWlsZXIKPDwgL1NpemUgOCAvUm9vdCAxIDAgUiA+PgpzdGFydHhyZWYKNzk4CiUlRU9GCg==",
  "base64",
);

test("a private conversation answers with a citation, keeps its turns and can be deleted", async ({ page, request }) => {
  const email = `conv-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const account = await (
    await request.post(`${API}/api/v1/auth/register`, { data: { email, password, organization_name: "Conv E2E" } })
  ).json();
  const auth = { Authorization: `Bearer ${account.access_token}` };
  const org = account.organization_id;
  const project = await (
    await request.post(`${API}/api/v1/projects?organization_id=${org}&name=Conv`, { headers: auth })
  ).json();
  const upload = await request.post(`${API}/api/v1/documents?organization_id=${org}&project_id=${project.id}`, {
    headers: auth,
    multipart: { file: { name: "politica_conv.pdf", mimeType: "application/pdf", buffer: policy } },
  });
  expect(upload.status()).toBe(202);
  const question = "Cuantos dias de vacaciones tienen los empleados?";
  await expect
    .poll(
      async () => {
        const res = await request.post(`${API}/api/v1/ask?organization_id=${org}`, { headers: auth, data: { question } });
        return res.ok() ? (await res.json()).abstained : `HTTP ${res.status()}`;
      },
      { timeout: 60_000, intervals: [2_000] },
    )
    .toBe(false);

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/conversations");
  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("Question").fill(question);
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByText("23 dias laborables").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("link", { name: "politica_conv.pdf, p.2" })).toBeVisible();
  await expect(page.getByRole("button", { name: /2 messages/ })).toBeVisible();

  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("button", { name: /2 messages/ })).toHaveCount(0);
  const listed = await request.get(`${API}/api/v1/conversations?organization_id=${org}`, { headers: auth });
  expect(await listed.json()).toEqual([]);
});

test("a new version of a document is uploaded from its page and reprocessed", async ({ page, request }) => {
  const email = `version-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const account = await (
    await request.post(`${API}/api/v1/auth/register`, { data: { email, password, organization_name: "Version E2E" } })
  ).json();
  const auth = { Authorization: `Bearer ${account.access_token}` };
  const org = account.organization_id;
  const project = await (
    await request.post(`${API}/api/v1/projects?organization_id=${org}&name=Version`, { headers: auth })
  ).json();
  const upload = await request.post(`${API}/api/v1/documents?organization_id=${org}&project_id=${project.id}`, {
    headers: auth,
    multipart: { file: { name: "politica_v1.pdf", mimeType: "application/pdf", buffer: policy } },
  });
  const documentId: string = (await upload.json()).document_id;
  await expect
    .poll(async () => (await (await request.get(
      `${API}/api/v1/documents/${documentId}/status?organization_id=${org}`, { headers: auth })).json()).status,
    { timeout: 60_000, intervals: [2_000] })
    .toBe("COMPLETED");

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto(`/documents/${documentId}`);
  // Same content as the current version: the API answers "unchanged" and the page says so.
  await page.getByLabel(/Upload a new version/).setInputFiles({ name: "politica_v1.pdf", mimeType: "application/pdf", buffer: policy });
  await expect(page.getByText("That file is the current version already.")).toBeVisible({ timeout: 15_000 });
  const changed = Buffer.concat([policy, Buffer.from("\n% v2\n")]);
  await page.getByLabel(/Upload a new version/).setInputFiles({ name: "politica_v2.pdf", mimeType: "application/pdf", buffer: changed });
  await expect(page.getByText(/New version queued/)).toBeVisible({ timeout: 15_000 });
  await expect
    .poll(async () => (await (await request.get(`${API}/api/v1/documents/${documentId}?organization_id=${org}`, {
      headers: auth })).json()).filename, { timeout: 30_000 })
    .toBe("politica_v2.pdf");
});
