import { expect, test, type Page } from "@playwright/test";

async function refreshProviderUntil(page: Page, text: string) {
  await expect(async () => {
    await page.getByRole("button", { name: "Refresh queues" }).click();
    await expect(page.getByText(text).first()).toBeVisible();
  }).toPass({ timeout: 15_000, intervals: [500, 1000] });
}

test("runs a real Graph interrupt and exposes completed Provider queues", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Follow one decision/i })).toBeVisible();

  await page.getByRole("button", { name: /Automatic refund/i }).click();
  await page.getByRole("button", { name: "Run through Graph" }).click();
  await expect(page.getByText("Execution paused")).toBeVisible();
  await expect(
    page.getByRole("paragraph").filter({ hasText: /Are you sure you want a refund/i })
  ).toBeVisible();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("Execution paused")).toBeHidden();

  await page.reload();
  await page.getByRole("button", { name: /Provider lifecycle/i }).click();
  await page.getByRole("button", { name: "Run through Graph" }).click();
  await expect(page.getByText("Execution paused")).toBeVisible();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("Execution paused")).toBeHidden();

  await page.getByLabel("Acting persona").selectOption("sup-1");
  await page.getByRole("button", { name: "Provider Ops" }).click();
  await refreshProviderUntil(page, "published");
  await refreshProviderUntil(page, "processed");
});

test("keeps assignment eligibility behind the supervisor API", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Bilingual complaint/i }).click();
  await page.getByRole("button", { name: "Run through Graph" }).click();

  await page.getByLabel("Acting persona").selectOption("sup-1");
  await page.getByRole("button", { name: "Case queue" }).click();
  await page.getByRole("button", { name: "Refresh queue" }).click();
  await expect(page.getByText("staff conduct complaint").first()).toBeVisible();
  await page.getByRole("button", { name: "Assign active agent" }).first().click();
  await expect(page.getByText("agent-7").first()).toBeVisible();

  await page.getByRole("button", { name: "Test safe failure" }).first().click();
  await expect(page.getByRole("alert")).toContainText("404");
});

test("shows a persisted transient failure followed by recovery", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Retry & recovery/i }).click();
  await page.getByRole("button", { name: "Run through Graph" }).click();
  await expect(page.getByText("Execution paused")).toBeVisible();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("Execution paused")).toBeHidden();

  await page.getByLabel("Acting persona").selectOption("sup-1");
  await page.getByRole("button", { name: "Provider Ops" }).click();
  await refreshProviderUntil(page, "HTTP 500");
  await expect(page.getByText("retry_scheduled").first()).toBeVisible();

  await refreshProviderUntil(page, "accepted");
  await refreshProviderUntil(page, "processed");
});
