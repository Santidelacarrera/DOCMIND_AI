import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const password = "correct-horse-battery-staple";

test("user can compare processed documents and generate a project summary on /intelligence", async ({
  page,
  request,
}) => {
  const email = `intel-e2e-${Date.now()}@example.com`;
  const regRes = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Intelligence E2E" },
  });
  expect(regRes.ok()).toBeTruthy();
  const account = (await regRes.json()) as { access_token: string; organization_id: string };
  const auth = { Authorization: `Bearer ${account.access_token}` };
  const orgId = account.organization_id;

  const projRes = await request.post(
    `${API}/api/v1/projects?organization_id=${orgId}&name=${encodeURIComponent("Contracts E2E")}`,
    { headers: auth },
  );
  expect(projRes.ok()).toBeTruthy();
  const project = (await projRes.json()) as { id: string };

  const up1 = await request.post(
    `${API}/api/v1/documents?organization_id=${orgId}&project_id=${project.id}`,
    {
      headers: auth,
      multipart: {
        file: {
          name: "contract-a.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("Contract A. Monthly rent is USD 1200. Effective date 2026-01-01. Notice period 30 days."),
        },
      },
    },
  );
  expect(up1.status()).toBe(202);

  const up2 = await request.post(
    `${API}/api/v1/documents?organization_id=${orgId}&project_id=${project.id}`,
    {
      headers: auth,
      multipart: {
        file: {
          name: "contract-b.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("Contract B. Monthly rent is USD 1500. Effective date 2026-02-01. Notice period 60 days."),
        },
      },
    },
  );
  expect(up2.status()).toBe(202);

  await expect
    .poll(
      async () => {
        const res = await request.get(`${API}/api/v1/documents?organization_id=${orgId}`, { headers: auth });
        if (!res.ok()) return 0;
        const docs = (await res.json()) as { status: string }[];
        return docs.filter((d) => d.status === "COMPLETED").length;
      },
      { timeout: 30_000 },
    )
    .toBe(2);

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/intelligence");
  await expect(
    page.getByRole("heading", { name: "Document comparison & contradiction detection" }),
  ).toBeVisible();

  await expect(page.getByLabel("contract-a.txt")).toBeVisible();
  await expect(page.getByLabel("contract-b.txt")).toBeVisible();
  await page.getByLabel("contract-a.txt").check();
  await page.getByLabel("contract-b.txt").check();
  await page.getByLabel("Focus topic (optional)").fill("rent");
  await page.getByRole("button", { name: "Compare selected" }).click();

  await expect(page.getByRole("heading", { name: /Comparison \(/ })).toBeVisible();

  await page.getByRole("button", { name: "Summarize project" }).click();
  await expect(page.getByText("From 2 document(s):")).toBeVisible();
});
