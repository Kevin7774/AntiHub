export type Route =
  | { type: "create" }
  | {
      type: "case";
      caseId: string;
    }
  | { type: "workspace" }
  | { type: "billing" }
  | { type: "admin_billing" };

export type DocHeading = {
  id: string;
  text: string;
  level: number;
};

export type DocTemplateCheck = {
  label: string;
  found: boolean;
};

export type StepStatus = "done" | "active" | "pending" | "failed";

export type StepItem = {
  key: string;
  label: string;
  status: StepStatus;
};

export const PIPELINE_STEPS = ["clone", "build", "run", "analyze", "showcase"] as const;

export const DOC_TEMPLATE: Array<{ label: string; match: RegExp[] }> = [
  { label: "概览/背景", match: [/overview/i, /简介/, /概览/, /背景/] },
  { label: "快速开始", match: [/quickstart/i, /快速开始/, /入门/] },
  { label: "安装部署", match: [/install/i, /安装/, /setup/i, /构建/] },
  { label: "运行/使用", match: [/usage/i, /运行/, /启动/, /使用/] },
  { label: "配置", match: [/config/i, /配置/, /env/i] },
  { label: "端口/访问", match: [/port/i, /端口/, /访问/] },
  { label: "问题排查", match: [/troubleshoot/i, /问题/, /故障/, /error/i, /错误/] },
  { label: "溯源/参考", match: [/sources/i, /来源/, /参考/] },
];

export function joinUrl(base: string, path: string) {
  const trimmed = base.replace(/\/+$/, "");
  return `${trimmed}${path}`;
}

export function toWsUrl(base: string) {
  if (!base || base.startsWith("/")) {
    return base;
  }
  if (base.startsWith("https://")) {
    return base.replace("https://", "wss://");
  }
  if (base.startsWith("http://")) {
    return base.replace("http://", "ws://");
  }
  return `ws://${base.replace(/^\/*/, "")}`;
}

export function parseRoute(pathname: string): Route {
  if (pathname.startsWith("/admin/billing")) {
    return { type: "admin_billing" };
  }
  if (pathname.startsWith("/workspace")) {
    return { type: "workspace" };
  }
  if (pathname.startsWith("/billing")) {
    return { type: "billing" };
  }
  if (pathname.startsWith("/create")) {
    return { type: "create" };
  }
  const match = pathname.match(/^\/cases\/([^/]+)$/);
  if (match && match[1]) {
    return { type: "case", caseId: match[1] };
  }
  return { type: "create" };
}

export function slugifyHeading(text: string) {
  const cleaned = text
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || "section";
}

export function extractHeadings(markdown: string): DocHeading[] {
  const lines = markdown.split(/\r?\n/);
  let inCode = false;
  const counts: Record<string, number> = {};
  const headings: DocHeading[] = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      inCode = !inCode;
      continue;
    }
    if (inCode) continue;
    const match = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (!match) continue;
    const level = match[1].length;
    const text = match[2].trim();
    if (!text) continue;
    const base = slugifyHeading(text);
    const count = (counts[base] ?? 0) + 1;
    counts[base] = count;
    const id = count > 1 ? `${base}-${count}` : base;
    headings.push({ id, text, level });
  }
  return headings;
}

export function buildDocTemplate(toc: DocHeading[]): DocTemplateCheck[] {
  return DOC_TEMPLATE.map((item) => {
    const found = toc.some((heading) => item.match.some((regex) => regex.test(heading.text)));
    return { label: item.label, found };
  });
}

export function buildPipelineSteps(
  stageLabels: Record<string, string>,
  stage?: string | null,
  status?: string | null
): StepItem[] {
  const normalizedStage = (stage || "").toLowerCase();
  const normalizedStatus = (status || "").toUpperCase();
  const stageIndex = PIPELINE_STEPS.findIndex((key) => normalizedStage.includes(key));
  return PIPELINE_STEPS.map((key, index) => {
    if (normalizedStatus === "FINISHED" || normalizedStatus === "SHOWCASE_READY") {
      return { key, label: stageLabels[key] || key, status: "done" };
    }
    if (stageIndex < 0) {
      return { key, label: stageLabels[key] || key, status: "pending" };
    }
    if (index < stageIndex) {
      return { key, label: stageLabels[key] || key, status: "done" };
    }
    if (index === stageIndex) {
      if (normalizedStatus === "FAILED" || normalizedStatus === "SHOWCASE_FAILED") {
        return { key, label: stageLabels[key] || key, status: "failed" };
      }
      return { key, label: stageLabels[key] || key, status: "active" };
    }
    return { key, label: stageLabels[key] || key, status: "pending" };
  });
}

export function getApiDebugDetail(apiBase: string, origin?: string) {
  const base = apiBase || "<same-origin>";
  let resolvedOrigin = origin;
  if (!resolvedOrigin && typeof window !== "undefined") {
    resolvedOrigin = window.location.origin;
  }
  return `API_BASE=${base} origin=${resolvedOrigin || "-"}`;
}

export function getFetchFailureHint(err: unknown, apiBase: string, origin?: string) {
  if (err instanceof TypeError && /Failed to fetch/i.test(err.message)) {
    return {
      message: "无法连接服务，请确认后端已启动并且地址正确。",
      detail: getApiDebugDetail(apiBase, origin),
    };
  }
  return null;
}

export function splitNdjsonBuffer(buffer: string): { lines: string[]; rest: string } {
  const normalized = String(buffer || "");
  const parts = normalized.split("\n");
  const rest = parts.pop() || "";
  const lines = parts.map((line) => line.trim()).filter(Boolean);
  return { lines, rest };
}

export function parseJsonLine<T>(line: string): T | null {
  const raw = String(line || "").trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}
