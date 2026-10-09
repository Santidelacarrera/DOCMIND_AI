import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

const pdf = Buffer.from(
  "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgNjEyIDc5MiBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=",
  "base64",
);

test("upload, extraction edit, and XLSX export use the real processing pipeline", async ({ page, request }) => {
  const email = `document-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const registered = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Document E2E" },
  });
  const account = await registered.json();
  const project = await request.post(
    `${API}/api/v1/projects?organization_id=${account.organization_id}&name=E2E`,
    { headers: { Authorization: `Bearer ${account.access_token}` } },
  );
  expect(project.status()).toBe(201);

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto("/documents");
  await expect(page.getByLabel("Project", { exact: true })).toBeVisible();
  // TODO(temporary): selectOption on this locator hangs for the full 30s
  // timeout with no other clue why, even though the same locator resolved
  // fine one line above. Dump the live DOM so the CI log says what's
  // actually there instead of us guessing from a trace we can't open here.
  // eslint-disable-next-line no-console
  console.log(
    "DEBUG selects:",
    JSON.stringify(
      await page.evaluate(() =>
        Array.from(document.querySelectorAll("select")).map((select) => ({
          id: select.id,
          name: select.name,
          ariaLabel: select.getAttribute("aria-label"),
          ariaLabelledby: select.getAttribute("aria-labelledby"),
          labels: Array.from(select.labels ?? []).map((label) => label.textContent),
          optionCount: select.options.length,
          outerHTML: select.outerHTML.slice(0, 400),
        })),
      ),
    ),
  );
  await page.getByLabel("Project", { exact: true }).selectOption({ label: "E2E" });
  await page.getByLabel("PDF").setInputFiles({ name: "test-document.pdf", mimeType: "application/pdf", buffer: pdf });
  await page.getByRole("button", { name: "Upload and process" }).click();
  await expect(page.getByRole("status")).toHaveText("Document queued for processing.");
  const documentLink = page.getByRole("link", { name: "test-document.pdf" });
  await expect(documentLink).toBeVisible();
  await documentLink.click();
  await expect(page.getByText("customer_name")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Juan Pérez")).toBeVisible();
  const customerField = page.locator(".field").filter({ hasText: "customer_name" });
  await customerField.getByRole("button", { name: "Edit" }).click();
  await customerField.getByRole("textbox").fill("Pedro González");
  await customerField.getByRole("button", { name: "Save" }).click();
  await page.reload();
  await expect(page.getByText("Pedro González")).toBeVisible({ timeout: 20_000 });
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "XLSX" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);
  expect((await download.createReadStream())?.readable).toBeTruthy();
});
