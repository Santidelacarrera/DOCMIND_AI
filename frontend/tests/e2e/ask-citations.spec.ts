import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

// A fictitious two-page policy; page 2 holds the fact the question asks for.
const policy = Buffer.from(
  "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFs1IDAgUiA3IDAgUl0gL0NvdW50IDIgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhIC9FbmNvZGluZyAvV2luQW5zaUVuY29kaW5nID4+CmVuZG9iago0IDAgb2JqCjw8IC9MZW5ndGggMTA5ID4+CnN0cmVhbQpCVAovRjEgMTEgVGYKMTggVEwKNTYgNzQwIFRkCihQT0xJVElDQSBERSBWQUNBQ0lPTkVTKSBUaiBUKgooRG9jdW1lbnRvIGZpY3RpY2lvIHBhcmEgbGEgcHJ1ZWJhIGUyZS4pIFRqIFQqCkVUCmVuZHN0cmVhbQplbmRvYmoKNSAwIG9iago8PCAvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDYxMiA3OTJdIC9SZXNvdXJjZXMgPDwgL0ZvbnQgPDwgL0YxIDMgMCBSID4+ID4+IC9Db250ZW50cyA0IDAgUiA+PgplbmRvYmoKNiAwIG9iago8PCAvTGVuZ3RoIDExMSA+PgpzdHJlYW0KQlQKL0YxIDExIFRmCjE4IFRMCjU2IDc0MCBUZAooTG9zIGVtcGxlYWRvcyB0aWVuZW4gZGVyZWNobyBhIDIzIGRpYXMgbGFib3JhYmxlcyBkZSB2YWNhY2lvbmVzIGFsIGFuby4pIFRqIFQqCkVUCmVuZHN0cmVhbQplbmRvYmoKNyAwIG9iago8PCAvVHlwZSAvUGFnZSAvUGFyZW50IDIgMCBSIC9NZWRpYUJveCBbMCAwIDYxMiA3OTJdIC9SZXNvdXJjZXMgPDwgL0ZvbnQgPDwgL0YxIDMgMCBSID4+ID4+IC9Db250ZW50cyA2IDAgUiA+PgplbmRvYmoKeHJlZgowIDgKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDE1IDAwMDAwIG4gCjAwMDAwMDAwNjQgMDAwMDAgbiAKMDAwMDAwMDEyNyAwMDAwMCBuIAowMDAwMDAwMjI0IDAwMDAwIG4gCjAwMDAwMDAzODQgMDAwMDAgbiAKMDAwMDAwMDUxMCAwMDAwMCBuIAowMDAwMDAwNjcyIDAwMDAwIG4gCnRyYWlsZXIKPDwgL1NpemUgOCAvUm9vdCAxIDAgUiA+PgpzdGFydHhyZWYKNzk4CiUlRU9GCg==",
  "base64",
);

test("the Ask page quotes the source, names document, page and fragment, and abstains without evidence", async ({
  page,
  request,
}) => {
  const email = `ask-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const account = await (
    await request.post(`${API}/api/v1/auth/register`, {
      data: { email, password, organization_name: "Ask E2E" },
    })
  ).json();
  const auth = { Authorization: `Bearer ${account.access_token}` };
  const org = account.organization_id;
  const project = await (
    await request.post(`${API}/api/v1/projects?organization_id=${org}&name=Ask`, { headers: auth })
  ).json();
  const upload = await request.post(`${API}/api/v1/documents?organization_id=${org}&project_id=${project.id}`, {
    headers: auth,
    multipart: { file: { name: "politica_e2e.pdf", mimeType: "application/pdf", buffer: policy } },
  });
  expect(upload.status()).toBe(202);
  const documentId: string = (await upload.json()).document_id;

  // Processing and then indexing run in the worker: wait until the API can answer.
  const question = "Cuantos dias de vacaciones tienen los empleados?";
  await expect
    .poll(
      async () => {
        const res = await request.post(`${API}/api/v1/ask?organization_id=${org}`, {
          headers: auth,
          data: { question },
        });
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

  await page.goto("/ask");
  await page.getByLabel("Question").fill(question);
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByRole("heading", { name: "Answer" })).toBeVisible({ timeout: 15_000 });
  const source = page.getByRole("link", { name: "politica_e2e.pdf, p.2" });
  await expect(source).toBeVisible();
  await expect(page.locator("blockquote")).toContainText("23 dias laborables");
  await expect(page.getByText(/fragment \d+/)).toBeVisible();

  await page.getByLabel("Question").fill("Cual es el color favorito del gerente?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByRole("heading", { name: "No answer found" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "Sources" })).toHaveCount(0);

  // The source link opens the cited document.
  await page.goto("/ask");
  await page.getByLabel("Question").fill(question);
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByRole("link", { name: "politica_e2e.pdf, p.2" }).click();
  await expect(page).toHaveURL(new RegExp(`/documents/${documentId}$`));
});
