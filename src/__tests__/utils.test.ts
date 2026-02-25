import { describe, expect, it } from "vitest";
import {
  buildDocTemplate,
  buildPipelineSteps,
  extractHeadings,
  getFetchFailureHint,
  joinUrl,
  parseJsonLine,
  parseRoute,
  splitNdjsonBuffer,
  slugifyHeading,
  toWsUrl,
} from "../utils";

describe("utils", () => {
  it("joinUrl builds same-origin paths", () => {
    expect(joinUrl("", "/cases")).toBe("/cases");
    expect(joinUrl("http://127.0.0.1:8010/", "/cases")).toBe("http://127.0.0.1:8010/cases");
    expect(joinUrl("/api", "/cases")).toBe("/api/cases");
  });

  it("toWsUrl handles same-origin and schemes", () => {
    expect(toWsUrl("")).toBe("");
    expect(toWsUrl("/api")).toBe("/api");
    expect(toWsUrl("http://example.com")).toBe("ws://example.com");
    expect(toWsUrl("https://example.com")).toBe("wss://example.com");
    expect(toWsUrl("localhost:8010")).toBe("ws://localhost:8010");
  });

  it("parseRoute resolves routes", () => {
    expect(parseRoute("/create")).toEqual({ type: "create" });
    expect(parseRoute("/cases/c_demo")).toEqual({ type: "case", caseId: "c_demo" });
    expect(parseRoute("/billing")).toEqual({ type: "billing" });
    expect(parseRoute("/billing/anything")).toEqual({ type: "billing" });
    expect(parseRoute("/workspace")).toEqual({ type: "workspace" });
    expect(parseRoute("/workspace/overview")).toEqual({ type: "workspace" });
    expect(parseRoute("/admin/billing")).toEqual({ type: "admin_billing" });
    expect(parseRoute("/admin/billing/plans")).toEqual({ type: "admin_billing" });
    expect(parseRoute("/")).toEqual({ type: "create" });
    expect(parseRoute("/cases/c_demo/extra")).toEqual({ type: "create" });
  });

  it("slugifyHeading normalizes headings", () => {
    expect(slugifyHeading("Hello World")).toBe("hello-world");
    expect(slugifyHeading("中文 标题")).toBe("中文-标题");
  });

  it("extractHeadings ignores code blocks and handles duplicates", () => {
    const markdown = [
      "# 标题",
      "正文",
      "```",
      "# not heading",
      "```",
      "## 标题",
      "## 标题",
    ].join("\n");
    const headings = extractHeadings(markdown);
    expect(headings).toHaveLength(3);
    expect(headings[0]).toMatchObject({ text: "标题", level: 1, id: "标题" });
    expect(headings[1]).toMatchObject({ text: "标题", level: 2, id: "标题-2" });
    expect(headings[2]).toMatchObject({ text: "标题", level: 2, id: "标题-3" });
  });

  it("buildDocTemplate marks matched sections", () => {
    const toc = extractHeadings("# 概览\n## 快速开始\n## 安装部署\n");
    const template = buildDocTemplate(toc);
    const quickstart = template.find((item) => item.label === "快速开始");
    const setup = template.find((item) => item.label === "安装部署");
    expect(quickstart?.found).toBe(true);
    expect(setup?.found).toBe(true);
  });

  it("buildPipelineSteps reflects stage and status", () => {
    const labels = { clone: "克隆", build: "构建", run: "运行", analyze: "分析", showcase: "展厅" };
    const steps = buildPipelineSteps(labels, "build", "BUILDING");
    expect(steps[0].status).toBe("done");
    expect(steps[1].status).toBe("active");
    expect(steps[2].status).toBe("pending");
    const failed = buildPipelineSteps(labels, "run", "FAILED");
    expect(failed[2].status).toBe("failed");
  });

  it("getFetchFailureHint returns debug detail", () => {
    const hint = getFetchFailureHint(new TypeError("Failed to fetch"), "", "http://localhost:5173");
    expect(hint?.message).toContain("无法连接服务");
    expect(hint?.detail).toContain("API_BASE=<same-origin>");
    expect(hint?.detail).toContain("origin=http://localhost:5173");
    expect(getFetchFailureHint(new Error("boom"), "", "http://localhost:5173")).toBeNull();
  });

  it("splitNdjsonBuffer extracts complete lines and carry", () => {
    const parsed = splitNdjsonBuffer('{"a":1}\n{"b":2}\n{"c":');
    expect(parsed.lines).toEqual(['{"a":1}', '{"b":2}']);
    expect(parsed.rest).toBe('{"c":');
  });

  it("parseJsonLine handles valid and invalid json", () => {
    expect(parseJsonLine<{ a: number }>('{"a":1}')?.a).toBe(1);
    expect(parseJsonLine("not-json")).toBeNull();
    expect(parseJsonLine("")).toBeNull();
  });
});
