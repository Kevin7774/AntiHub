import { expect, test, type Page } from "@playwright/test";
import fs from "fs";
import path from "path";

const INDEX_HTML = fs.readFileSync(path.resolve(process.cwd(), "dist/index.html"), "utf-8");

const demoCase = {
  case_id: "c_demo",
  status: "RUNNING",
  stage: "run",
  repo_url: "https://github.com/example/repo",
  resolved_ref: "main",
  commit_sha: "deadbeef",
  runtime: {
    access_url: "http://127.0.0.1:30000",
    host_port: 30000,
    container_id: "container-123",
  },
  env_keys: ["API_KEY", "TOKEN"],
  updated_at: 1700000000,
  created_at: 1699990000,
  manual_status: "SUCCESS",
  manual_generated_at: 1700000000,
};

const manualResponse = {
  case_id: "c_demo",
  manual_markdown: ["# 概览", "## 快速开始", "## 安装部署", "### 配置"].join("\n"),
  meta: {
    generated_at: 1700000000,
    generator_version: "v0.4",
    signals: { has_readme: true, has_dockerfile: true, tree_depth: 2, file_count: 12 },
    time_cost_ms: 16,
  },
};

type MockBaseOptions = {
  templates?: Array<{ name: string; repo_url?: string }>;
  cases?: { items: typeof demoCase[]; total: number; page: number; size: number };
  detail?: typeof demoCase;
  manualStatus?: { case_id: string; status: string; generated_at?: number | null };
  manual?: typeof manualResponse;
};

async function mockEndpoints(page: Page, options: MockBaseOptions = {}) {
  const detail = options.detail ?? demoCase;
  const manualStatus =
    options.manualStatus ?? { case_id: detail.case_id, status: "SUCCESS", generated_at: 1700000000 };
  const manual = options.manual ?? manualResponse;
  await page.route("**/error-codes", (route) => route.fulfill({ json: {} }));
  await page.route("**/case-templates", (route) =>
    route.fulfill({ json: options.templates ?? [] })
  );
  await page.route(`**/cases/${detail.case_id}/manual/status`, (route) => {
    if (route.request().resourceType() === "document") return route.fallback();
    return route.fulfill({ json: manualStatus });
  });
  await page.route(`**/cases/${detail.case_id}/manual`, (route) => {
    if (route.request().resourceType() === "document") return route.fallback();
    return route.fulfill({ json: manual });
  });
  await page.route(`**/cases/${detail.case_id}`, (route) => {
    if (route.request().resourceType() === "document") {
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: INDEX_HTML,
      });
    }
    return route.fulfill({ json: detail });
  });
  await page.route(/\/cases\?.*$/, (route) => {
    if (route.request().resourceType() === "document") return route.fallback();
    return route.fulfill({
      json: options.cases ?? { items: [], total: 0, page: 1, size: 10 },
    });
  });
  await page.route(/\/cases$/, (route) => {
    if (route.request().resourceType() === "document") return route.fallback();
    if (route.request().method() === "POST") {
      return route.fulfill({ json: detail });
    }
    return route.fulfill({
      json: options.cases ?? { items: [], total: 0, page: 1, size: 10 },
    });
  });
}

test("控制台加载案例列表", async ({ page }) => {
  await mockEndpoints(page, { cases: { items: [demoCase], total: 1, page: 1, size: 10 } });
  await page.goto("/");
  await expect(page.getByTestId("page-dashboard")).toBeVisible();
  await expect(page.getByTestId("page-title-dashboard")).toBeVisible();
  await expect(page.getByTestId("case-card-c_demo")).toBeVisible();
});

test("模板库可加载模板列表", async ({ page }) => {
  await mockEndpoints(page, {
    templates: [{ name: "hello-world", repo_url: "https://github.com/example/hello" }],
  });
  await page.goto("/create");
  await expect(page.getByTestId("page-create")).toBeVisible();
  await expect(page.getByTestId("page-title-library")).toBeVisible();
  await expect(page.getByTestId("template-select")).toBeVisible();
  await expect(page.locator("select[data-testid=\"template-select\"] option[value=\"hello-world\"]")).toHaveCount(1);
});

test("案例详情可切换 Tabs", async ({ page }) => {
  await mockEndpoints(page);
  await page.goto("/cases/c_demo");
  await expect(page.getByTestId("page-title-detail")).toBeVisible();
  await expect(page.getByTestId("case-header")).toBeVisible();
  await page.getByTestId("tab-logs").click();
  await expect(page.getByTestId("panel-logs")).toBeVisible();
  await page.getByTestId("tab-manual").click();
  await expect(page.getByTestId("panel-manual")).toBeVisible();
});

test("说明书 TOC 渲染", async ({ page }) => {
  await mockEndpoints(page);
  await page.goto("/cases/c_demo");
  await page.getByTestId("tab-manual").click();
  const toc = page.getByTestId("manual-toc");
  await expect(toc).toBeVisible();
  await expect(toc.locator("a")).toHaveCount(4);
});
