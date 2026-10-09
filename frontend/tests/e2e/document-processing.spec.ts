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

  // TODO(temporary): the GET /api/v1/projects request succeeds (200, per the
  // api container's own access log) but the "Project" <select> still shows
  // zero matching options for the full 5s wait that replaced the original
  // 30s hang. Log the actual response body the browser received, to settle
  // whether the project list really comes back empty or whether this is a
  // render-side bug instead.
  page.on("response", (response) => {
    if (response.url().includes("/api/v1/projects")) {
      response
        .text()
        .then((body) => console.log(`DEBUG projects response [${response.status()}]: ${body}`))
        .catch((err) => console.log(`DEBUG projects response read failed: ${err}`));
    }
  });
  // The fetch succeeds with the right body (confirmed above), so if the
  // option still never renders, something in the page's own JS must be
  // throwing after that -- forward the actual browser console/errors,
  // which nothing currently surfaces into the test output.
  page.on("console", (msg) => console.log(`PAGE CONSOLE [${msg.type()}]: ${msg.text()}`));
  page.on("pageerror", (err) => console.log(`PAGE ERROR: ${err}`));

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto("/documents");
  const projectSelect = page.getByLabel("Project", { exact: true });
  await expect(projectSelect).toBeVisible();
  // The select is visible as soon as the page mounts, but it starts out with
  // zero <option>s -- the project list is still loading (GET /organizations,
  // then GET /projects) when this assertion passes, since it only checks the
  // (empty) <select> itself. selectOption does not poll for a matching
  // <option> to show up later: given zero options it just waits on the
  // locator resolution step forever and times out, even though the option
  // does arrive a moment later. Wait for the real option first -- via a
  // plain CSS/text locator, not getByRole("option"): Playwright's role
  // engine doesn't reliably see native <option> elements (confirmed: the
  // accessibility snapshot captured at the exact moment getByRole("option")
  // gave up already showed `option "E2E" [selected]` in the combobox).
  // TODO(temporary): widening the wait to 20s (44 straight 0-element polls)
  // ruled out plain CI slowness -- this is a genuinely stuck state, not a
  // slow one, despite the fetch response being confirmed correct and fast
  // with no console/page error logged. Bypass Playwright's locator/role
  // machinery entirely and poll the raw DOM directly via page.evaluate to
  // see what the <select> actually contains over time, from ground truth.
  let selectHtml = "";
  for (let i = 0; i < 20; i++) {
    selectHtml = await page.evaluate(
      () => document.querySelector("select")?.outerHTML ?? "NO SELECT FOUND",
    );
    console.log(`DEBUG raw select outerHTML at t=${i}s: ${selectHtml}`);
    if (selectHtml.includes("E2E")) break;
    await page.waitForTimeout(1000);
  }
  await expect(projectSelect.locator("option", { hasText: "E2E" })).toHaveCount(1, { timeout: 5_000 });
  await projectSelect.selectOption({ label: "E2E" });
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
