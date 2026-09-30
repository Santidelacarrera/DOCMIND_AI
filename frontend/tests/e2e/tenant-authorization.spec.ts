import { expect, test } from "@playwright/test";

const pdf = Buffer.from(
  "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgNjEyIDc5MiBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=",
  "base64",
);

test("a second tenant cannot read or export a manipulated document id", async ({ page, request }) => {
  const password = "correct-horse-battery-staple";
  const suffix = Date.now();
  const a = await request.post("http://localhost:8000/api/v1/auth/register", { data: { email: `a-${suffix}@example.com`, password, organization_name: "Tenant A" } });
  const accountA = await a.json();
  const project = await request.post(`http://localhost:8000/api/v1/projects?organization_id=${accountA.organization_id}&name=A`, { headers: { Authorization: `Bearer ${accountA.access_token}` } });
  const projectA = await project.json();
  const uploaded = await request.post(`http://localhost:8000/api/v1/documents?organization_id=${accountA.organization_id}&project_id=${projectA.id}`, { headers: { Authorization: `Bearer ${accountA.access_token}` }, multipart: { file: { name: "tenant-a.pdf", mimeType: "application/pdf", buffer: pdf } } });
  expect(uploaded.status()).toBe(202);
  const documentId = (await uploaded.json()).document_id;
  const bEmail = `b-${suffix}@example.com`;
  const b = await request.post("http://localhost:8000/api/v1/auth/register", { data: { email: bEmail, password, organization_name: "Tenant B" } });
  const accountB = await b.json();
  const headers = { Authorization: `Bearer ${accountB.access_token}` };
  expect([403, 404]).toContain((await request.get(`http://localhost:8000/api/v1/documents/${documentId}?organization_id=${accountB.organization_id}`, { headers })).status());
  expect([403, 404]).toContain((await request.get(`http://localhost:8000/api/v1/documents/${documentId}/export?organization_id=${accountB.organization_id}&format=xlsx`, { headers })).status());
  await page.goto("/login");
  await page.getByLabel("Email").fill(bEmail);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto(`/documents/${documentId}`);
  await expect(page.getByText("Extraction is not ready yet")).toBeVisible();
  await expect(page.getByText("tenant-a.pdf")).toHaveCount(0);
});
