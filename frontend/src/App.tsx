import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import mermaid from "mermaid";
import { QRCodeCanvas } from "qrcode.react";
import KnowledgeGraphWorkbench from "./KnowledgeGraphWorkbench";
import { parseKnowledgeGraphAsset, type KnowledgeGraph, type KnowledgeGraphAnalysis } from "./knowledge-graph";
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
  type Route,
  type StepItem,
} from "./utils";

const API_BASE = import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || "http://127.0.0.1:8010";

const AUTH_TOKEN_STORAGE_KEY = "antihub_access_token";

type AuthUserInfo = {
  username: string;
  role: string;
  tenant_id?: string | null;
  tenant_code?: string | null;
  tenant_name?: string | null;
};

type AuthLoginResponse = {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUserInfo;
};

type BillingPlan = {
  plan_id: string;
  code: string;
  name: string;
  description?: string | null;
  currency: string;
  price_cents: number;
  monthly_points: number;
  active: boolean;
  billing_cycle?: string | null;
  trial_days?: number | null;
  metadata?: Record<string, unknown> | null;
};

type BillingPlanEntitlement = {
  entitlement_id: string;
  plan_id: string;
  key: string;
  enabled: boolean;
  value?: unknown;
  limit?: number | null;
  metadata?: Record<string, unknown>;
};

const BILLING_FALLBACK_PLANS: BillingPlan[] = [
  {
    plan_id: "seed_monthly_198",
    code: "monthly_198",
    name: "月度订阅",
    description: "适合高频日常协作，快速交付关键分析。",
    currency: "cny",
    price_cents: 19800,
    monthly_points: 10000,
    active: true,
  },
  {
    plan_id: "seed_quarterly_398",
    code: "quarterly_398",
    name: "季度订阅",
    description: "热门方案，兼顾稳定产出与成本效率。",
    currency: "cny",
    price_cents: 39800,
    monthly_points: 30000,
    active: true,
  },
  {
    plan_id: "seed_yearly_1980",
    code: "yearly_1980",
    name: "年度订阅",
    description: "适合长期项目与管理层持续决策场景。",
    currency: "cny",
    price_cents: 198000,
    monthly_points: 150000,
    active: true,
  },
];

function cloneBillingFallbackPlans(): BillingPlan[] {
  return BILLING_FALLBACK_PLANS.map((item) => ({ ...item }));
}

function normalizePlansOrFallback(rows: BillingPlan[] | null | undefined): BillingPlan[] {
  if (Array.isArray(rows) && rows.length > 0) {
    return rows;
  }
  return cloneBillingFallbackPlans();
}

type BillingCheckoutResponse = {
  provider: string;
  checkout_url: string;
  checkout_payload?: Record<string, unknown>;
  order_id: string;
  external_order_id: string;
};

type BillingSubscriptionSnapshot = {
  subscription_id?: string | null;
  status: string;
  plan_code?: string | null;
  plan_name?: string | null;
  expires_at?: string | null;
};

type BillingPointsSnapshot = {
  user_id: string;
  balance: number;
};

type BillingPointHistoryItem = {
  flow_id: string;
  flow_type: string;
  points: number;
  balance_after?: number | null;
  note?: string | null;
  order_id?: string | null;
  subscription_id?: string | null;
  occurred_at?: string | null;
};

type AdminUserBillingStatus = {
  username: string;
  role: string;
  active: boolean;
  tenant_id?: string | null;
  tenant_code?: string | null;
  tenant_name?: string | null;
  subscription: BillingSubscriptionSnapshot;
  points_balance: number;
};

type TenantInfo = {
  tenant_id: string;
  code: string;
  name: string;
  active: boolean;
};

type TenantWorkspacePlanBrief = {
  code: string;
  name: string;
  currency: string;
  price_cents: number;
  monthly_points: number;
  active: boolean;
};

type TenantWorkspaceSnapshot = {
  user: AuthUserInfo;
  tenant?: TenantInfo | null;
  member_count: number;
  subscription: BillingSubscriptionSnapshot;
  points: BillingPointsSnapshot;
  available_plans: TenantWorkspacePlanBrief[];
};

type BillingOrder = {
  order_id: string;
  user_id: string;
  plan_id: string;
  plan_code?: string | null;
  provider: string;
  external_order_id?: string | null;
  amount_cents: number;
  currency: string;
  status: string;
  created_at?: string | null;
  paid_at?: string | null;
};

type BillingMyOrderStatus = {
  order_id: string;
  external_order_id: string;
  status: string;
  plan_code?: string | null;
  amount_cents: number;
  currency: string;
  created_at?: string | null;
  paid_at?: string | null;
};

type BillingAuditLog = {
  log_id: string;
  occurred_at: string;
  provider: string;
  event_type: string;
  external_event_id?: string | null;
  external_order_id?: string | null;
  signature_valid: boolean;
  outcome: string;
  detail?: string | null;
};

type BillingAuditLogDetail = BillingAuditLog & {
  signature?: string | null;
  raw_payload: string;
};

type AuthStatus = "checking" | "authenticated" | "unauthenticated";

type AuthContextValue = {
  status: AuthStatus;
  token: string | null;
  user: AuthUserInfo | null;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string, tenantName: string, tenantCode: string) => Promise<void>;
  logout: () => void;
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  buildWsUrl: (path: string) => string;
};

const AuthContext = React.createContext<AuthContextValue | null>(null);

function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) {
    throw new Error("AuthContext is missing");
  }
  return ctx;
}

function safeStorageGet(key: string): string | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeStorageSet(key: string, value: string) {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function safeStorageRemove(key: string) {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

async function parseAuthError(response: Response, fallback: string): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: string; message?: string };
    const detail = String(data?.detail || data?.message || "").trim();
    if (detail) return detail;
  } catch {
    // ignore
  }
  try {
    const text = (await response.text()).trim();
    if (text) return text;
  } catch {
    // ignore
  }
  return fallback;
}

function resolveWsBase(apiBase: string) {
  const wsBase = toWsUrl(apiBase);
  // Absolute ws:// or wss:// base.
  if (wsBase && !wsBase.startsWith("/")) {
    return wsBase;
  }
  // Same-origin or path base: prefix with current origin.
  const origin =
    typeof window !== "undefined" && window.location?.origin ? window.location.origin : "http://127.0.0.1:5173";
  const wsOrigin = toWsUrl(origin);
  return joinUrl(wsOrigin, wsBase || "");
}

type RuntimeInfo = {
  container_id?: string | null;
  host_port?: number | null;
  access_url?: string | null;
  started_at?: number | null;
  exited_at?: number | null;
  exit_code?: number | null;
  ports?: number[] | null;
  services?: string[] | null;
};

type CaseResponse = {
  case_id: string;
  status: string;
  stage: string;
  mode?: string | null;
  run_mode?: string | null;
  commit_sha?: string | null;
  resolved_ref?: string | null;
  analyze_status?: string | null;
  resolved_dockerfile_path?: string | null;
  resolved_context_path?: string | null;
  runtime?: RuntimeInfo | null;
  env_keys?: string[];
  error_code?: string | null;
  error_message?: string | null;
  created_at?: number | null;
  updated_at?: number | null;
  repo_url?: string | null;
  ref?: string | null;
  branch?: string | null;
  archived?: boolean;
  archived_at?: number | null;
  attempt?: number | null;
  retry_of?: string | null;
  manual_status?: string | null;
  manual_generated_at?: number | null;
  manual_error_code?: string | null;
  manual_error_message?: string | null;
  report_ready?: boolean;
  report_cached?: boolean | null;
  analyze_error_code?: string | null;
  analyze_error_message?: string | null;
  visual_status?: string | null;
  visual_ready?: boolean;
  visual_cached?: boolean | null;
  visual_error_code?: string | null;
  visual_error_message?: string | null;
  repo_type?: string | null;
  repo_evidence?: string[];
  strategy_selected?: string | null;
  strategy_reason?: string | null;
  fallback_reason?: string | null;
  generated_files?: string[];
  default_account?: string | null;
};

type CaseListResponse = {
  items: CaseResponse[];
  total: number;
  page: number;
  size: number;
};

type CaseActionResponse = {
  case_id: string;
  action: string;
  status: string;
  message: string;
};

type LogEntry = {
  ts: number | string;
  stream: string;
  level: string;
  line: string;
};

type Toast = {
  type: "success" | "error";
  message: string;
  detail?: string;
  code?: string;
  copyLabel?: string;
};

type ErrorCodeMap = Record<string, { message: string; hint: string }>;

type ManualMeta = {
  generated_at: number;
  generator_version: string;
  repo_fingerprint?: string;
  similarity_score?: number | null;
  warnings?: string[];
  signals: Record<string, unknown>;
  time_cost_ms: number;
};

type ManualStatusResponse = {
  case_id: string;
  status: string;
  generated_at?: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

type ManualResponse = {
  case_id: string;
  manual_markdown: string;
  meta: ManualMeta;
};

type EvidenceClaim = {
  claim: string;
  evidence: Evidence;
  confidence: number;
};

type ProductStory = {
  hook?: {
    headline?: EvidenceClaim | null;
    subline?: EvidenceClaim | null;
  };
  problem_context?: {
    target_user?: EvidenceClaim | null;
    pain_point?: EvidenceClaim | null;
    current_bad_solution?: EvidenceClaim | null;
  };
  what_this_repo_gives_you?: EvidenceClaim[];
  usage_scenarios?: EvidenceClaim[];
  why_it_matters_now?: EvidenceClaim | null;
  next_step_guidance?: {
    if_you_are_a_builder?: EvidenceClaim | null;
    if_you_are_a_pm_or_founder?: EvidenceClaim | null;
    if_you_are_evaluating?: EvidenceClaim | null;
  };
  meta?: {
    skipped_claims?: { label?: string; reason?: string }[];
    source?: string;
    reason_code?: string | null;
    error?: string | null;
    evidence_catalog?: Evidence[];
  };
};

type VisualFile = {
  name: string;
  url: string;
  mime?: string | null;
};

type VisualAsset = {
  kind: string;
  status: string;
  files: VisualFile[];
  meta?: Record<string, unknown>;
  created_at?: number;
  error_code?: string | null;
  error_message?: string | null;
};

type VisualsResponse = {
  case_id: string;
  commit_sha: string;
  status: string;
  assets: VisualAsset[];
  created_at: number;
  cached?: boolean | null;
  template_version?: string | null;
};

type RepoIndex = {
  readme_summary?: { text?: string | null };
  tree?: { count?: number; entries?: string[] };
};

type IngestMeta = {
  repo_meta_available?: boolean | null;
  repo_meta_reason?: string | null;
};

type HealthReport = {
  status?: string;
  api?: string;
  redis?: string;
  docker?: string;
  openclaw?: string;
  details?: Record<string, string>;
  version?: string;
  api_host?: string;
  api_port?: number;
};

type RecommendScoreMetric = {
  label: string;
  score: number;
  status?: string | null;
  value?: string | null;
  detail?: string | null;
};

type RecommendHealth = {
  overall_score: number;
  grade: string;
  activity: RecommendScoreMetric;
  community: RecommendScoreMetric;
  maintenance: RecommendScoreMetric;
  warnings?: string[];
  signals?: string[];
};

type RecommendCapability = {
  code: string;
  name: string;
  weight: number;
};

type RecommendScoreBreakdown = {
  relevance: number;
  popularity: number;
  cost_bonus: number;
  capability_match: number;
  final_score: number;
};

type RecommendAction = {
  action_type: string;
  label: string;
  url?: string | null;
  deploy_supported?: boolean;
  detail?: string | null;
};

type RecommendItem = {
  id: string;
  full_name: string;
  html_url: string;
  description?: string | null;
  language?: string | null;
  topics?: string[];
  stars?: number;
  forks?: number;
  open_issues?: number;
  license?: string | null;
  archived?: boolean | null;
  pushed_at?: string | null;
  updated_days?: number | null;
  match_score?: number;
  match_reasons?: string[];
  match_tags?: string[];
  risk_notes?: string[];
  health?: RecommendHealth;
  source?: string | null;
  product_type?: string | null;
  official_url?: string | null;
  repo_url?: string | null;
  capabilities?: RecommendCapability[];
  score_breakdown?: RecommendScoreBreakdown | null;
  action?: RecommendAction | null;
  deployment_mode?: string | null;
};

type RecommendProfile = {
  summary?: string | null;
  search_query?: string | null;
  keywords?: string[];
  must_have?: string[];
  nice_to_have?: string[];
  target_stack?: string[];
  scenarios?: string[];
};

type RecommendCitation = {
  id: string;
  source: string;
  title: string;
  url: string;
  snippet?: string | null;
  score?: number | null;
  reason?: string | null;
};

type RecommendResponse = {
  request_id: string;
  query?: string | null;
  mode: string;
  generated_at: number;
  requirement_excerpt?: string | null;
  search_query?: string | null;
  profile?: RecommendProfile | null;
  warnings?: string[];
  sources?: string[];
  deep_summary?: string | null;
  insight_points?: string[];
  trace_steps?: string[];
  citations?: RecommendCitation[];
  recommendations: RecommendItem[];
};

type RecommendStreamEvent =
  | { type: "thought"; message?: string | null }
  | { type: "result"; data?: RecommendResponse | null }
  | { type: "error"; message?: string | null };

type UnderstandStatusResponse = {
  case_id: string;
  repo_url?: string | null;
  state: string;
  message: string;
  visual_status?: string | null;
  visual_ready?: boolean;
  visual_error_code?: string | null;
  visual_error_message?: string | null;
  updated_at?: number | null;
};

type UnderstandResultResponse = {
  case_id: string;
  repo_url?: string | null;
  state: string;
  status: string;
  message: string;
  assets: VisualAsset[];
  created_at: number;
  cached?: boolean | null;
};

type RepoGraph = {
  nodes?: { id: string; label?: string; x?: number; y?: number; type?: string }[];
  edges?: { source: string; target: string; relation?: string; weight?: number }[];
  meta?: { truncated?: boolean };
};

type EvidenceLineRange = { start: number; end: number };

type EvidenceSource = {
  kind: string;
  file?: string;
  section?: string;
  symbol?: string;
  imports?: string[];
  call_graph?: { from?: string; to?: string };
  line_range?: EvidenceLineRange;
};

type Evidence = {
  id: string;
  type: string;
  sources: EvidenceSource[];
  derivation_rule: string;
  strength: "strong" | "medium" | "weak";
};

type Spotlights = {
  items?: {
    file_path: string;
    language?: string;
    snippet?: string;
    truncated?: boolean;
    start_line?: number;
    end_line?: number;
    line_range?: EvidenceLineRange;
    highlights?: { start_line: number; end_line: number; reason?: string }[];
    explanation?: string;
    evidence?: Evidence;
  }[];
};

type StoryboardShot = {
  evidence_id?: string | null;
};

type StoryboardScene = {
  id: string;
  duration?: number;
  shots?: StoryboardShot[];
};

type Storyboard = {
  scenes?: StoryboardScene[];
  total_duration?: number;
  evidence_catalog?: Evidence[];
  meta?: { skipped_scenes?: { scene_id?: string; reason?: string }[] };
};

type CaseTemplate = {
  template_id?: string;
  name: string;
  group?: string | null;
  description?: string | null;
  dimensions?: string[];
  expected?: {
    status?: string | null;
    error_code?: string | null;
    note?: string | null;
  } | null;
  what_to_verify?: string | null;
  repo_url?: string | null;
  git_url?: string | null;
  ref?: string | null;
  default_mode?: string | null;
  dockerfile_path?: string | null;
  context_path?: string | null;
  default_env_keys?: string[];
  build_mode?: string | null;
  port_mode?: string | null;
  timeouts?: Record<string, number>;
};

type DocSource = {
  label: string;
  detail: string;
  status: "ok" | "missing" | "info";
};

type DocAction = "ask" | "note" | "share" | "sources";

const ERROR_EXPLAIN: Record<string, string> = {
  GITHUB_RATE_LIMIT: "GitHub 访问过于频繁，请稍后再试。",
  GIT_CLONE_FAILED: "无法访问该仓库，请确认它是公开的 GitHub 仓库。",
  INGEST_REPO_NOT_FOUND: "无法访问该仓库，请确认它是公开的 GitHub 仓库。",
  INGEST_FAILED: "仓库读取失败，请稍后重试。",
  GIT_REF_NOT_FOUND: "分支或标签不存在，请改用默认分支。",
  LFS_FAILED: "大文件下载失败，但仍会展示可用内容。",
  SUBMODULE_FAILED: "子模块获取失败，但仍会展示可用内容。",
  VISUAL_NOT_READY: "讲解内容尚未生成，请稍后刷新。",
  REPORT_NOT_READY: "讲解内容尚未生成，请稍后刷新。",
  VISUALIZE_REPORT_NOT_READY: "讲解素材未准备好，请稍后刷新。",
  VISUALIZE_INDEX_FAILED: "仓库结构解析失败，但仍会展示可用内容。",
  VISUALIZE_GRAPH_FAILED: "结构图生成失败，但代码讲解仍然可用。",
  VISUALIZE_SPOTLIGHT_FAILED: "代码讲解生成失败，但其他内容仍然可用。",
  VISUALIZE_STORYBOARD_FAILED: "讲解脚本生成失败，但其他内容仍然可用。",
  VISUALIZE_PRODUCT_STORY_FAILED: "产品叙事生成失败，但其他内容仍然可用。",
  VISUALIZE_RENDER_FAILED: "视频生成失败，但结构图和代码讲解仍然可用。",
  VISUALIZE_IMAGE_API_FAILED: "配图生成失败，但结构图和代码讲解仍然可用。",
  VISUALIZE_MERMAID_RENDER_FAILED: "结构图生成失败，但代码讲解仍然可用。",
  VISUALIZE_INVALID_RESPONSE: "讲解生成失败，请稍后再试。",
  TIMEOUT_VISUALIZE: "生成时间较长，已尽量返回可用内容。",
  ANALYZE_LLM_FAILED: "讲解生成失败，请稍后再试。",
  TIMEOUT_ANALYZE: "生成时间较长，请稍后再试。",
  UNEXPECTED_ERROR: "发生未知问题，但我们仍会展示可用内容。",
  STOPPED_BY_USER: "任务已被停止，但仍会展示可用内容。",
};

const STREAM_LABELS: Record<string, string> = {
  build: "构建",
  run: "运行",
  analyze: "分析",
  visualize: "讲解",
  system: "系统",
};

const STAGE_LABELS: Record<string, string> = {
  clone: "克隆",
  build: "构建",
  run: "运行",
  analyze: "分析",
  system: "系统",
  showcase: "展厅",
};

const STATUS_LABELS: Record<string, string> = {
  PENDING: "待处理",
  CLONING: "克隆中",
  BUILDING: "构建中",
  STARTING: "启动中",
  RUNNING: "运行中",
  STOPPED: "已停止",
  FAILED: "失败",
  FINISHED: "已结束",
  ANALYZING: "分析中",
  SHOWCASE_READY: "展厅就绪",
  SHOWCASE_FAILED: "展厅失败",
};

function useRoute() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((path: string) => {
    window.history.pushState({}, "", path);
    setRoute(parseRoute(path));
  }, []);

  return { route, navigate };
}

function formatTime(ts?: number | string | null) {
  if (!ts) return "-";
  const value = typeof ts === "string" ? Number(ts) : ts;
  if (!value || Number.isNaN(value)) return "-";
  const date = new Date(value * 1000);
  return date.toLocaleString();
}

function formatNumber(value?: number | null) {
  if (value === null || value === undefined) return "-";
  const formatWithUnit = (num: number, unit: string) => {
    const raw = num.toFixed(1);
    const trimmed = raw.endsWith(".0") ? raw.slice(0, -2) : raw;
    return `${trimmed}${unit}`;
  };
  if (value >= 100000000) return formatWithUnit(value / 100000000, "亿");
  if (value >= 10000) return formatWithUnit(value / 10000, "万");
  if (value >= 1000) return formatWithUnit(value / 1000, "千");
  return String(value);
}

function productTypeLabel(productType?: string | null) {
  const normalized = String(productType || "").toLowerCase();
  if (normalized === "open_source") return "开源";
  if (normalized === "commercial") return "商业SaaS";
  if (normalized === "private_solution") return "私有化方案";
  return "方案";
}

function productTypePillClass(productType?: string | null) {
  const normalized = String(productType || "").toLowerCase();
  if (normalized === "open_source") return "pill-success";
  if (normalized === "commercial") return "pill-warn";
  if (normalized === "private_solution") return "pill-muted";
  return "pill-muted";
}

function statusTone(status?: string) {
  const normalized = (status || "").toUpperCase();
  if (normalized === "RUNNING") return "running";
  if (normalized === "FAILED") return "failed";
  if (normalized === "SHOWCASE_FAILED") return "failed";
  if (normalized === "STOPPED") return "neutral";
  if (normalized === "BUILDING" || normalized === "CLONING" || normalized === "STARTING") {
    return "working";
  }
  if (normalized === "SHOWCASE_READY") return "finished";
  if (normalized === "FINISHED") return "finished";
  return "neutral";
}

function streamLabel(stream: string) {
  return STREAM_LABELS[stream] || stream;
}

function statusLabel(status?: string) {
  if (!status) return "-";
  const normalized = status.toUpperCase();
  const label = STATUS_LABELS[normalized];
  return label || normalized;
}

function stageLabel(stage?: string) {
  if (!stage) return "-";
  const normalized = stage.toLowerCase();
  const label = STAGE_LABELS[normalized];
  return label || stage;
}

function manualStatusLabel(status?: string | null) {
  const normalized = (status || "").toUpperCase();
  if (!normalized || normalized === "NOT_STARTED") return "未生成";
  if (normalized === "PENDING" || normalized === "RUNNING") return "生成中";
  if (normalized === "SUCCESS") return "已生成";
  if (normalized === "FAILED") return "生成失败";
  return normalized;
}

function analyzeStatusLabel(status?: string | null) {
  const normalized = (status || "").toUpperCase();
  if (!normalized || normalized === "PENDING") return "待分析";
  if (normalized === "RUNNING") return "分析中";
  if (normalized === "FINISHED") return "已完成";
  if (normalized === "FAILED") return "分析失败";
  return normalized;
}

function visualStatusLabel(status?: string | null) {
  const normalized = (status || "").toUpperCase();
  if (!normalized || normalized === "PENDING" || normalized === "NOT_STARTED") return "未生成";
  if (normalized === "RUNNING") return "生成中";
  if (normalized === "SUCCESS") return "已生成";
  if (normalized === "PARTIAL") return "部分失败";
  if (normalized === "FAILED") return "生成失败";
  return normalized;
}

function formatToastDetail(entries: Record<string, unknown>, extra?: string) {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(entries)) {
    if (value === null || value === undefined) continue;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (!trimmed) continue;
      parts.push(`${key}=${trimmed}`);
      continue;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      parts.push(`${key}=${value}`);
      continue;
    }
    parts.push(`${key}=${JSON.stringify(value)}`);
  }
  if (extra) {
    parts.push(extra);
  }
  return parts.join("\n");
}

function resolveErrorHint(code: string | null | undefined, errorCodes: ErrorCodeMap) {
  if (!code) return null;
  if (errorCodes[code]) {
    return `${errorCodes[code].message} ${errorCodes[code].hint}`.trim();
  }
  return ERROR_EXPLAIN[code] || null;
}
function deriveSources(meta?: ManualMeta | null): DocSource[] {
  if (!meta) return [];
  const signals = meta.signals || {};
  const sourceList: DocSource[] = [];
  const hasReadme = Boolean((signals as Record<string, unknown>).has_readme);
  const hasDockerfile = Boolean((signals as Record<string, unknown>).has_dockerfile);
  const treeDepth = (signals as Record<string, unknown>).tree_depth;
  const fileCount = (signals as Record<string, unknown>).file_count;

  sourceList.push({
    label: "README 文档",
    detail: hasReadme ? "已解析" : "未发现",
    status: hasReadme ? "ok" : "missing",
  });
  sourceList.push({
    label: "Dockerfile 文件",
    detail: hasDockerfile ? "已解析" : "未发现",
    status: hasDockerfile ? "ok" : "missing",
  });
  if (treeDepth !== undefined) {
    sourceList.push({
      label: "仓库结构",
      detail: `深度 ${treeDepth}`,
      status: "info",
    });
  }
  if (fileCount !== undefined) {
    sourceList.push({
      label: "配置与清单",
      detail: `文件 ${fileCount}`,
      status: "info",
    });
  }
  return sourceList;
}

function LoginScreen({
  status,
  apiBase,
  onLogin,
  onRegister,
}: {
  status: AuthStatus;
  apiBase: string;
  onLogin: (username: string, password: string) => Promise<void>;
  onRegister: (username: string, password: string, tenantName: string, tenantCode: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [tenantCode, setTenantCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const user = username.trim();
      if (!user) {
        setError("请输入用户名。");
        return;
      }
      if (mode === "login") {
        await onLogin(user, password);
      } else {
        await onRegister(user, password, tenantName.trim(), tenantCode.trim());
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message || (mode === "login" ? "登录失败，请重试。" : "注册失败，请重试。"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-shell" data-testid="page-login">
      <section className="card auth-card">
        <div className="auth-mode-switch">
          <button
            className={`ghost ${mode === "login" ? "active" : ""}`}
            type="button"
            onClick={() => setMode("login")}
            disabled={submitting}
          >
            登录
          </button>
          <button
            className={`ghost ${mode === "register" ? "active" : ""}`}
            type="button"
            onClick={() => setMode("register")}
            disabled={submitting}
          >
            注册
          </button>
        </div>
        <div className="section-title">{mode === "login" ? "登录" : "注册并创建租户空间"}</div>
        <div className="muted auth-sub">
          API: <span className="mono">{apiBase || "<same-origin>"}</span>
        </div>
        {mode === "register" ? <div className="muted auth-sub">注册后会自动创建并绑定你的租户空间。</div> : null}
        {status === "checking" ? <div className="muted auth-status">正在检查登录状态…</div> : null}
        <form className="form auth-form" onSubmit={handleSubmit}>
          <label className="field">
            <span>用户名</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="admin"
              autoComplete="username"
              disabled={submitting}
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="password"
              autoComplete="current-password"
              disabled={submitting}
            />
          </label>
          {mode === "register" ? (
            <>
              <label className="field">
                <span>租户名称</span>
                <input
                  value={tenantName}
                  onChange={(event) => setTenantName(event.target.value)}
                  placeholder="例如：Zed Studio"
                  autoComplete="organization"
                  disabled={submitting}
                />
              </label>
              <label className="field">
                <span>租户代号（可选）</span>
                <input
                  value={tenantCode}
                  onChange={(event) => setTenantCode(event.target.value)}
                  placeholder="例如：zed-studio"
                  autoComplete="off"
                  disabled={submitting}
                />
              </label>
            </>
          ) : null}
          {error ? <div className="error-banner auth-error">{error}</div> : null}
          <button className="primary" type="submit" disabled={submitting || status === "checking"}>
            {submitting ? (mode === "login" ? "登录中…" : "注册中…") : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>
      </section>
    </div>
  );
}

function planIntervalLabel(code: string) {
  const normalized = (code || "").toLowerCase();
  if (normalized.includes("year") || normalized.includes("annual")) return "年付";
  if (normalized.includes("quarter") || normalized.includes("qtr")) return "季付";
  if (normalized.includes("month")) return "月付";
  return "订阅";
}

type BillingPlanTier = "monthly" | "quarterly" | "yearly" | "other";

const BILLING_PLAN_PRIORITY: Record<Exclude<BillingPlanTier, "other">, string[]> = {
  monthly: ["plan_monthly", "commercial_monthly", "vip_monthly", "monthly", "month"],
  quarterly: ["plan_quarterly", "commercial_quarterly", "vip_quarterly", "quarterly", "quarter", "qtr"],
  yearly: ["plan_yearly", "commercial_yearly", "vip_yearly", "yearly", "annual", "year"],
};

function billingPlanTier(code: string): BillingPlanTier {
  const normalized = (code || "").trim().toLowerCase();
  if (normalized.includes("year") || normalized.includes("annual")) return "yearly";
  if (normalized.includes("quarter") || normalized.includes("qtr")) return "quarterly";
  if (normalized.includes("month")) return "monthly";
  return "other";
}

function billingPlanRank(plan: BillingPlan, tier: Exclude<BillingPlanTier, "other">): number {
  const normalized = (plan.code || "").trim().toLowerCase();
  const preferred = BILLING_PLAN_PRIORITY[tier];
  for (let i = 0; i < preferred.length; i += 1) {
    const marker = preferred[i];
    if (normalized === marker) return i;
    if (normalized.includes(marker)) return i + 10;
  }
  return 999;
}

function compactPlanPrice(cents: number, currency: string): string {
  const code = (currency || "").trim().toLowerCase() || "cny";
  const raw = Number(cents || 0) / 100;
  const pretty = Number.isInteger(raw) ? String(raw) : raw.toFixed(2).replace(/\.00$/, "");
  if (code === "cny") return `${pretty}￥`;
  if (code === "usd") return `$${pretty}`;
  if (code === "sgd") return `S$${pretty}`;
  return `${pretty} ${code.toUpperCase()}`.trim();
}

function planTierTitle(plan: BillingPlan, tier: BillingPlanTier): string {
  if (tier === "monthly") return "月付";
  if (tier === "quarterly") return "季付";
  if (tier === "yearly") return "年付";
  return plan.name || "订阅";
}

function planTierTagline(tier: BillingPlanTier, fallback: string): string {
  if (tier === "monthly") return "灵活开通，适合快速上手。";
  if (tier === "quarterly") return "性价比更高，适合稳定增长。";
  if (tier === "yearly") return "长期投入最优，企业优先选择。";
  return fallback || "升级后可获得更多配额与积分权益。";
}

function planTierPeriod(tier: BillingPlanTier): string {
  if (tier === "monthly") return "月";
  if (tier === "quarterly") return "季";
  if (tier === "yearly") return "年";
  return "期";
}

function planTierFeatures(tier: BillingPlanTier, points: number): string[] {
  const pointsText = `购买即获 ${formatNumber(points)} 积分`;
  if (tier === "monthly") {
    return ["扫码支付，秒级开通", pointsText, "支持随时续费升级"];
  }
  if (tier === "quarterly") {
    return ["季度方案更省心", pointsText, "适合持续运营场景"];
  }
  if (tier === "yearly") {
    return ["年度投入，综合成本更低", pointsText, "推荐给长期项目与团队"];
  }
  return [pointsText, "支付成功后自动激活", "支持扫码支付"];
}

function hasAdminAccess(role: string): boolean {
  const normalized = String(role || "").trim().toLowerCase();
  return normalized === "admin" || normalized === "root";
}

function formatPointDelta(points: number): string {
  const value = Number(points || 0);
  if (value > 0) return `+${formatNumber(value)}`;
  if (value < 0) return `-${formatNumber(Math.abs(value))}`;
  return "0";
}

function pointHistoryLabel(item: BillingPointHistoryItem): string {
  const flowType = String(item.flow_type || "").toLowerCase();
  const note = String(item.note || "").trim();
  if (flowType === "grant") return "充值入账";
  if (flowType === "refund") return "退款扣回";
  if (flowType === "expire") return "积分过期";
  if (flowType === "adjust") return "人工调整";
  if (flowType === "consume") {
    if (note.startsWith("deep_search")) return "深度搜索";
    if (note.startsWith("one_click_deploy")) return "一键部署";
    return "积分消耗";
  }
  return flowType || "积分流水";
}

function AccessDeniedCard({
  title,
  detail,
  actionLabel,
  onAction,
}: {
  title: string;
  detail: string;
  actionLabel: string;
  onAction: () => void;
}) {
  return (
    <div className="create-grid">
      <div className="card">
        <div className="section-title">{title}</div>
        <div className="error-banner">{detail}</div>
        <div className="row-actions">
          <button className="primary" type="button" onClick={onAction}>
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function TenantWorkspacePage({
  apiFetch,
  role,
  pushToast,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  role: string;
  pushToast: (toast: Toast) => void;
}) {
  const [snapshot, setSnapshot] = useState<TenantWorkspaceSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [tenants, setTenants] = useState<TenantInfo[]>([]);
  const [pointHistory, setPointHistory] = useState<BillingPointHistoryItem[]>([]);
  const [tenantName, setTenantName] = useState("");
  const [tenantCode, setTenantCode] = useState("");
  const [creatingTenant, setCreatingTenant] = useState(false);
  const isAdmin = hasAdminAccess(role);
  const isRoot = String(role || "").toLowerCase() === "root";

  const refresh = useCallback(async () => {
    if (loading) return;
    setLoading(true);
    try {
      const workspaceResp = await apiFetch("/tenant/workspace");
      if (!workspaceResp.ok) {
        const message = await parseAuthError(workspaceResp, `workspace load failed (${workspaceResp.status})`);
        throw new Error(message);
      }
      const workspace = (await workspaceResp.json()) as TenantWorkspaceSnapshot;
      setSnapshot(workspace);

      const historyResp = await apiFetch("/billing/points/history/me?limit=30");
      if (historyResp.ok) {
        const rows = (await historyResp.json()) as BillingPointHistoryItem[];
        setPointHistory(Array.isArray(rows) ? rows : []);
      } else {
        setPointHistory([]);
      }

      if (isAdmin) {
        const tenantResp = await apiFetch("/admin/tenants?include_inactive=true");
        if (tenantResp.ok) {
          const rows = (await tenantResp.json()) as TenantInfo[];
          setTenants(Array.isArray(rows) ? rows : []);
        }
      } else {
        setTenants([]);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "工作台加载失败", detail: message });
    } finally {
      setLoading(false);
    }
  }, [apiFetch, isAdmin, loading, pushToast]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const onPointsUpdated = () => {
      refresh();
    };
    window.addEventListener("antihub:points-updated", onPointsUpdated);
    return () => window.removeEventListener("antihub:points-updated", onPointsUpdated);
  }, [refresh]);

  const createTenant = useCallback(async () => {
    if (!isAdmin || creatingTenant) return;
    const name = tenantName.trim();
    if (!name) {
      pushToast({ type: "error", message: "租户名称不能为空" });
      return;
    }
    setCreatingTenant(true);
    try {
      const response = await apiFetch("/admin/tenants", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          code: tenantCode.trim() || undefined,
          active: true,
        }),
      });
      if (!response.ok) {
        const message = await parseAuthError(response, `create tenant failed (${response.status})`);
        throw new Error(message);
      }
      pushToast({ type: "success", message: "租户已创建" });
      setTenantName("");
      setTenantCode("");
      await refresh();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "创建租户失败", detail: message });
    } finally {
      setCreatingTenant(false);
    }
  }, [apiFetch, creatingTenant, isAdmin, pushToast, refresh, tenantCode, tenantName]);

  const planPreview = useMemo(() => {
    if (!snapshot) return [];
    const activePlans = snapshot.available_plans.filter((item) => item.active);
    return activePlans
      .sort((a, b) => a.price_cents - b.price_cents || a.code.localeCompare(b.code))
      .slice(0, 3);
  }, [snapshot]);

  return (
    <div className="create-grid" data-testid="page-workspace">
      <div className="card card-hero">
        <div className="detail-head">
          <div className="detail-head-left">
            <div>
              <div className="section-title">租户工作台</div>
              <div className="section-sub">统一身份、订阅状态、积分和租户协作入口。</div>
            </div>
            <div className="row-actions">
              <button className="ghost" type="button" onClick={refresh} disabled={loading}>
                {loading ? "刷新中…" : "刷新"}
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="workspace-grid">
        <div className="card card-glow">
          <div className="section-title">租户身份</div>
          <div className="kv-row">
            <span className="kv-key">用户</span>
            <span className="kv-value mono">{snapshot?.user.username || "-"}</span>
          </div>
          <div className="kv-row">
            <span className="kv-key">角色</span>
            <span className="kv-value">
              <span className={`pill ${hasAdminAccess(snapshot?.user.role || "user") ? "pill-warn" : "pill-muted"}`}>
                {snapshot?.user.role || "-"}
              </span>
            </span>
          </div>
          <div className="kv-row">
            <span className="kv-key">租户</span>
            <span className="kv-value">{snapshot?.tenant?.name || snapshot?.user.tenant_name || "未绑定租户"}</span>
          </div>
          <div className="kv-row">
            <span className="kv-key">租户代号</span>
            <span className="kv-value mono">{snapshot?.tenant?.code || snapshot?.user.tenant_code || "-"}</span>
          </div>
          <div className="kv-row">
            <span className="kv-key">成员数</span>
            <span className="kv-value mono">{snapshot?.member_count ?? "-"}</span>
          </div>
        </div>

        <div className="card card-glow">
          <div className="section-title">订阅与积分</div>
          <div className="kv-row">
            <span className="kv-key">订阅</span>
            <span className="kv-value">
              {snapshot?.subscription.status === "active" ? (
                <span className="pill pill-success">生效中</span>
              ) : (
                <span className="pill pill-muted">未订阅</span>
              )}
            </span>
          </div>
          <div className="kv-row">
            <span className="kv-key">当前套餐</span>
            <span className="kv-value mono">{snapshot?.subscription.plan_name || snapshot?.subscription.plan_code || "-"}</span>
          </div>
          <div className="kv-row">
            <span className="kv-key">到期</span>
            <span className="kv-value mono">{snapshot?.subscription.expires_at || "-"}</span>
          </div>
          <div className="kv-row">
            <span className="kv-key">积分余额</span>
            <span className="kv-value mono">{snapshot ? formatNumber(snapshot.points.balance) : "-"}</span>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="section-title">可用套餐预览</div>
        <div className="workspace-plan-list">
          {planPreview.length ? (
            planPreview.map((plan) => (
              <div className="workspace-plan-item" key={plan.code}>
                <div>
                  <div className="mono">{plan.name}</div>
                  <div className="muted mono">{plan.code}</div>
                </div>
                <div className="workspace-plan-meta">
                  <span className="mono">{compactPlanPrice(plan.price_cents, plan.currency)}</span>
                  <span className="pill pill-muted">{formatNumber(plan.monthly_points)} 积分</span>
                </div>
              </div>
            ))
          ) : (
            <div className="muted">暂无可用套餐。</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="section-title">账单明细</div>
        <div className="table billing-admin-table billing-history-table">
          <div className="table-header">
            <div className="cell">时间</div>
            <div className="cell">类型</div>
            <div className="cell">变动</div>
            <div className="cell">余额</div>
            <div className="cell">说明</div>
          </div>
          {pointHistory.map((item) => (
            <div className="table-row" key={item.flow_id}>
              <div className="cell mono">{item.occurred_at || "-"}</div>
              <div className="cell">{pointHistoryLabel(item)}</div>
              <div className="cell mono">{formatPointDelta(item.points)}</div>
              <div className="cell mono">{item.balance_after == null ? "-" : formatNumber(item.balance_after)}</div>
              <div className="cell muted mono">{item.note || "-"}</div>
            </div>
          ))}
          {!pointHistory.length ? <div className="muted">暂无积分流水。</div> : null}
        </div>
      </div>

      {isAdmin ? (
        <div className="card">
          <div className="section-title">租户管理（{isRoot ? "Root" : "Admin"}）</div>
          <div className="workspace-admin-grid">
            <div className="filters-panel">
              <label className="field">
                <span>租户名称</span>
                <input
                  value={tenantName}
                  onChange={(event) => setTenantName(event.target.value)}
                  placeholder="例如：Finance Team"
                  disabled={!isRoot}
                />
              </label>
              <label className="field">
                <span>租户代号（可选）</span>
                <input
                  value={tenantCode}
                  onChange={(event) => setTenantCode(event.target.value)}
                  placeholder="例如：finance-team"
                  disabled={!isRoot}
                />
              </label>
              {!isRoot ? <div className="muted">当前账号为租户管理员，仅可查看本租户信息。</div> : null}
              <div className="row-actions">
                <button className="primary" type="button" onClick={createTenant} disabled={creatingTenant || !isRoot}>
                  {creatingTenant ? "创建中…" : "创建租户"}
                </button>
              </div>
            </div>
            <div className="table billing-admin-table">
              <div className="table-header">
                <div className="cell">租户</div>
                <div className="cell">代号</div>
                <div className="cell">状态</div>
              </div>
              {tenants.map((tenant) => (
                <div className="table-row" key={tenant.tenant_id}>
                  <div className="cell">{tenant.name}</div>
                  <div className="cell mono">{tenant.code}</div>
                  <div className="cell">
                    <span className={`pill ${tenant.active ? "pill-success" : "pill-danger"}`}>
                      {tenant.active ? "active" : "disabled"}
                    </span>
                  </div>
                </div>
              ))}
              {!tenants.length ? <div className="muted">暂无租户</div> : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function BillingPage({
  apiFetch,
  role,
  pushToast,
  navigate,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  role: string;
  pushToast: (toast: Toast) => void;
  navigate: (path: string) => void;
}) {
  const [plans, setPlans] = useState<BillingPlan[]>(() => cloneBillingFallbackPlans());
  const [subscription, setSubscription] = useState<BillingSubscriptionSnapshot | null>(null);
  const [points, setPoints] = useState<BillingPointsSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [checkoutBusy, setCheckoutBusy] = useState<string | null>(null);
  const [modalState, setModalState] = useState<{
    open: boolean;
    planName: string;
    planCode: string;
    checkoutUrl: string;
    externalOrderId: string;
    amountText: string;
  }>({ open: false, planName: "", planCode: "", checkoutUrl: "", externalOrderId: "", amountText: "" });

  const loadPlans = useCallback(async () => {
    const response = await apiFetch("/billing/plans");
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `GET /billing/plans failed (${response.status})`);
    }
    const data = (await response.json()) as BillingPlan[];
    return data;
  }, [apiFetch]);

  const loadStatus = useCallback(async () => {
    const [subResp, pointsResp] = await Promise.all([apiFetch("/billing/subscription/me"), apiFetch("/billing/points/me")]);
    if (subResp.ok) {
      setSubscription((await subResp.json()) as BillingSubscriptionSnapshot);
    }
    if (pointsResp.ok) {
      setPoints((await pointsResp.json()) as BillingPointsSnapshot);
    }
  }, [apiFetch]);

  const refresh = useCallback(async () => {
    if (loading) return;
    setLoading(true);
    try {
      const data = await loadPlans();
      setPlans(normalizePlansOrFallback(Array.isArray(data) ? data : []));
      await loadStatus();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setPlans((prev) => (Array.isArray(prev) && prev.length > 0 ? prev : cloneBillingFallbackPlans()));
      pushToast({ type: "error", message: "会员数据加载失败", detail: message });
    } finally {
      setLoading(false);
    }
  }, [loading, loadPlans, loadStatus, pushToast]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const startCheckout = useCallback(
    async (plan: BillingPlan) => {
      if (checkoutBusy) return;
      setCheckoutBusy(plan.plan_id);
      try {
        const response = await apiFetch("/billing/checkout", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            plan_code: plan.code,
            idempotency_key: `ui:${plan.code}:${Date.now()}`,
          }),
        });
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || `checkout failed (${response.status})`);
        }
        const data = (await response.json()) as BillingCheckoutResponse;
        const url = String(data.checkout_url || "").trim();
        const externalOrderId = String(data.external_order_id || "").trim();
        if (!url || !externalOrderId) {
          throw new Error("后端未返回可用的 checkout_url/external_order_id");
        }
        setModalState({
          open: true,
          planName: plan.name,
          planCode: plan.code,
          checkoutUrl: url,
          externalOrderId,
          amountText: compactPlanPrice(plan.price_cents, plan.currency),
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        pushToast({ type: "error", message: "发起订阅失败", detail: `${message}。如需帮助请联系客服 3193773138@qq.com` });
      } finally {
        setCheckoutBusy(null);
      }
    },
    [apiFetch, checkoutBusy, pushToast]
  );

  const handlePaid = useCallback(async () => {
    setModalState((prev) => ({ ...prev, open: false }));
    pushToast({ type: "success", message: "支付成功，会员已激活" });
    await loadStatus();
  }, [loadStatus, pushToast]);

  const active = subscription?.status === "active";
  const activePlanCode = subscription?.plan_code || null;
  const visiblePlans = useMemo(() => {
    const source = hasAdminAccess(role) ? plans : plans.filter((item) => item.active);
    if (source.length > 0) return source;
    return cloneBillingFallbackPlans();
  }, [plans, role]);

  const showcasePlans = useMemo(() => {
    const picked: BillingPlan[] = [];
    const used = new Set<string>();
    const tiers: Array<Exclude<BillingPlanTier, "other">> = ["monthly", "quarterly", "yearly"];
    for (const tier of tiers) {
      const candidates = visiblePlans
        .filter((plan) => billingPlanTier(plan.code) === tier && !used.has(plan.plan_id))
        .sort(
          (a, b) =>
            billingPlanRank(a, tier) - billingPlanRank(b, tier) ||
            a.price_cents - b.price_cents ||
            a.name.localeCompare(b.name)
        );
      const winner = candidates[0];
      if (!winner) continue;
      picked.push(winner);
      used.add(winner.plan_id);
    }
    if (picked.length) return picked;
    return [...visiblePlans]
      .sort((a, b) => a.price_cents - b.price_cents || a.name.localeCompare(b.name))
      .slice(0, 3);
  }, [visiblePlans]);
  const hiddenPlanCount = Math.max(0, visiblePlans.length - showcasePlans.length);

  return (
    <div className="create-grid" data-testid="page-billing">
      <div className="card card-hero">
        <div className="detail-head">
          <div className="detail-head-left">
            <div>
              <div className="section-title">会员与订阅</div>
              <div className="section-sub">电脑端扫码支付（Native Pay），支付完成后页面会自动检测并刷新状态。</div>
            </div>
            <div className="row-actions">
              <button className="ghost" type="button" onClick={refresh} disabled={loading}>
                {loading ? "刷新中…" : "刷新"}
              </button>
            </div>
          </div>
        </div>
        <div className="overview-grid">
          <div className="card card-glow">
            <div className="section-title">当前状态</div>
            <div className="kv-row">
              <span className="kv-key">订阅</span>
              <span className="kv-value">
                {active ? <span className="pill pill-success">生效中</span> : <span className="pill pill-muted">未订阅</span>}
              </span>
            </div>
            <div className="kv-row">
              <span className="kv-key">套餐</span>
              <span className="kv-value mono">{subscription?.plan_name || activePlanCode || "-"}</span>
            </div>
            <div className="kv-row">
              <span className="kv-key">到期</span>
              <span className="kv-value mono">{subscription?.expires_at || "-"}</span>
            </div>
          </div>
          <div className="card card-glow">
            <div className="section-title">积分余额</div>
            <div className="billing-points">
              <div className="billing-points-value">{points ? formatNumber(points.balance) : "-"}</div>
              <div className="muted">支付成功后按套餐一次性发放积分。</div>
            </div>
          </div>
        </div>
      </div>

      <div className="card card-pricing-showcase">
        <div className="pricing-showcase-head">
          <div>
            <div className="section-title">升级套餐</div>
            <div className="muted">扫码支付开通会员，支付成功后积分即时到账。</div>
            <div className="trust-hint">
              安全支付 · 微信官方通道 · 如需帮助请联系 <a href="mailto:3193773138@qq.com">3193773138@qq.com</a>
              {" · "}
              <button type="button" className="text-link" onClick={() => navigate("/refund")}>退款政策</button>
            </div>
          </div>
        </div>
        {hasAdminAccess(role) ? (
          <div className="pricing-admin-tip">
            <span className="pill pill-warn">管理员视图</span>
            <span className="muted">
              {hiddenPlanCount > 0 ? `已自动聚合 ${hiddenPlanCount} 个非标准套餐，仅展示月/季/年三档。` : "当前仅展示月/季/年三档。"}
            </span>
          </div>
        ) : null}
        <div className="billing-grid billing-grid-showcase">
          {showcasePlans.length ? (
            showcasePlans.map((plan) => {
              const tier = billingPlanTier(plan.code);
              const isCurrent = active && activePlanCode === plan.code;
              const title = planTierTitle(plan, tier);
              const tagline = planTierTagline(tier, String(plan.description || ""));
              const features = planTierFeatures(tier, Number(plan.monthly_points || 0));
              return (
                <div
                  key={plan.plan_id}
                  className={`plan-card plan-card-dark plan-tier-${tier} ${isCurrent ? "plan-card-current" : ""}`}
                >
                  <div className="plan-head">
                    <div>
                      <div className="plan-title">{title}</div>
                      <div className="plan-tagline">{tagline}</div>
                    </div>
                    <div className="plan-badges">
                      <span className="pill pill-muted">{planIntervalLabel(plan.code)}</span>
                      {tier === "yearly" ? <span className="pill pill-warn">推荐</span> : null}
                      {isCurrent ? <span className="pill pill-success">当前</span> : null}
                      {!plan.active ? <span className="pill pill-danger">已下架</span> : null}
                    </div>
                  </div>
                  <div className="plan-price-line">
                    <span className="plan-price">{compactPlanPrice(plan.price_cents, plan.currency)}</span>
                    <span className="plan-price-unit">/ {planTierPeriod(tier)}</span>
                  </div>
                  <div className="plan-actions">
                    <button
                      className="primary plan-cta"
                      type="button"
                      onClick={() => startCheckout(plan)}
                      disabled={Boolean(checkoutBusy) || !plan.active}
                    >
                      {checkoutBusy === plan.plan_id ? "生成二维码…" : isCurrent ? "继续续费" : `升级至${title}`}
                    </button>
                  </div>
                  <div className="plan-feature-list">
                    {features.map((feature) => (
                      <div className="plan-feature-item" key={`${plan.plan_id}-${feature}`}>
                        <span className="plan-feature-dot" aria-hidden>
                          ✦
                        </span>
                        <span>{feature}</span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="muted">暂无套餐，请联系管理员在管理页创建或启用。</div>
          )}
        </div>
      </div>

      <LegalFooter navigate={navigate} />

      <PaymentModal
        open={modalState.open}
        planName={modalState.planName}
        planCode={modalState.planCode}
        checkoutUrl={modalState.checkoutUrl}
        externalOrderId={modalState.externalOrderId}
        amountText={modalState.amountText}
        apiFetch={apiFetch}
        onClose={() => setModalState((prev) => ({ ...prev, open: false }))}
        onPaid={handlePaid}
        pushToast={pushToast}
      />
    </div>
  );
}

function localizeOrderStatus(raw: string): string {
  const s = (raw || "").toLowerCase();
  if (s === "pending") return "等待支付";
  if (s === "paid") return "已支付";
  if (s === "failed") return "支付失败";
  if (s === "canceled") return "已取消";
  if (s === "refunded") return "已退款";
  return raw || "未知";
}

function PaymentModal({
  open,
  planName,
  planCode,
  checkoutUrl,
  externalOrderId,
  amountText,
  apiFetch,
  onClose,
  onPaid,
  pushToast,
}: {
  open: boolean;
  planName: string;
  planCode: string;
  checkoutUrl: string;
  externalOrderId: string;
  amountText?: string;
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  onClose: () => void;
  onPaid: () => void;
  pushToast: (toast: Toast) => void;
}) {
  const [polling, setPolling] = useState(false);
  const [status, setStatus] = useState<string>("pending");
  const [success, setSuccess] = useState(false);
  const timerRef = useRef<number | null>(null);
  const successTimerRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);
  const completedRef = useRef(false);

  const clearSuccessTimer = useCallback(() => {
    if (successTimerRef.current) {
      window.clearTimeout(successTimerRef.current);
      successTimerRef.current = null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setPolling(false);
    clearSuccessTimer();
  }, [clearSuccessTimer]);

  const normalizeOrderStatus = useCallback((value: string): string => {
    const normalized = String(value || "").trim().toLowerCase();
    return normalized || "pending";
  }, []);

  const checkOnce = useCallback(async () => {
    const key = encodeURIComponent(String(externalOrderId || "").trim());
    const response = await apiFetch(`/billing/orders/me/${key}/status`);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `order status check failed (${response.status})`);
    }
    const data = (await response.json()) as BillingMyOrderStatus;
    return normalizeOrderStatus(data.status || "pending");
  }, [apiFetch, externalOrderId, normalizeOrderStatus]);

  const finishAsPaid = useCallback(() => {
    if (completedRef.current) return;
    completedRef.current = true;
    stopPolling();
    setSuccess(true);
    clearSuccessTimer();
    successTimerRef.current = window.setTimeout(() => {
      onPaid();
    }, 900);
  }, [clearSuccessTimer, onPaid, stopPolling]);

  const checkNow = useCallback(async () => {
    if (completedRef.current) return;
    try {
      const next = await checkOnce();
      setStatus(next || "pending");
      if (next === "paid") {
        finishAsPaid();
        return;
      }
      if (next === "failed" || next === "canceled" || next === "refunded") {
        stopPolling();
        pushToast({ type: "error", message: "订单状态异常", detail: `当前订单状态：${next}` });
        return;
      }
      if (polling) {
        const elapsedMs = Date.now() - startedAtRef.current;
        if (elapsedMs > 10 * 60 * 1000) {
          stopPolling();
          pushToast({ type: "error", message: "检测超时", detail: "超过 10 分钟仍未检测到支付成功，可稍后点击手动校验。" });
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      stopPolling();
      pushToast({ type: "error", message: "状态检测失败", detail: `${message}。可点击手动校验重试，或联系客服 3193773138@qq.com` });
    }
  }, [checkOnce, finishAsPaid, polling, pushToast, stopPolling]);

  const startPolling = useCallback(() => {
    if (timerRef.current || completedRef.current) return;
    startedAtRef.current = Date.now();
    setPolling(true);
    const tick = () => {
      void checkNow();
    };
    timerRef.current = window.setInterval(tick, 2000);
    tick();
  }, [checkNow]);

  useEffect(() => {
    if (!open) {
      stopPolling();
      completedRef.current = false;
      setStatus("pending");
      setSuccess(false);
      return;
    }
    completedRef.current = false;
    startPolling();
    return () => stopPolling();
  }, [open, startPolling, stopPolling]);

  const simulatePaid = async () => {
    try {
      const response = await apiFetch("/billing/dev/simulate-payment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ external_order_id: externalOrderId }),
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `simulate failed (${response.status})`);
      }
      pushToast({ type: "success", message: "已模拟支付成功（Dev）" });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "模拟支付失败", detail: message });
    }
  };

  if (!open) return null;
  const url = String(checkoutUrl || "").trim();

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Payment">
      <div className={`card modal-card ${success ? "modal-success" : ""}`}>
        <div className="modal-head">
          <div>
            <div className="section-title">微信扫码支付</div>
            <div className="muted">
              套餐 <span className="mono">{planName || planCode || "-"}</span>
              {amountText ? <> · 应付 <span className="mono">{amountText}</span></> : null}
            </div>
          </div>
          <button className="ghost" type="button" onClick={onClose} disabled={polling && success}>
            关闭
          </button>
        </div>

        {url ? (
          <div className="payment-qr">
            <div className="payment-qr-frame" aria-hidden={success}>
              <QRCodeCanvas value={url} size={240} includeMargin />
            </div>
            <div className="payment-qr-tip">请使用微信扫一扫</div>
            <div className="payment-qr-meta">
              <span className="muted">订单</span>
              <span className="mono">{externalOrderId}</span>
              <CopyButton value={externalOrderId} label="复制订单号" />
            </div>
            <div className="payment-qr-meta">
              <span className="muted">链接</span>
              <span className="mono payment-url">{url}</span>
              <CopyButton value={url} label="复制链接" />
            </div>
          </div>
        ) : (
          <div className="error-banner">缺少二维码链接（checkout_url）。</div>
        )}

        <div className="modal-actions">
          <div className="muted">
            {success ? "支付已确认，正在同步…" : polling ? `检测中 · ${localizeOrderStatus(status)}` : `已暂停检测 · ${localizeOrderStatus(status)}`}
          </div>
          <div className="row-actions">
            <button className="ghost" type="button" onClick={() => void checkNow()} disabled={success}>
              我已完成支付，立即校验
            </button>
            <button
              className="ghost"
              type="button"
              onClick={() => {
                if (polling) {
                  stopPolling();
                } else {
                  startPolling();
                }
              }}
              disabled={success}
            >
              {polling ? "暂停自动检测" : "恢复自动检测"}
            </button>
            {import.meta.env.DEV ? (
              <button className="primary" type="button" onClick={simulatePaid} disabled={success}>
                模拟支付成功（Dev）
              </button>
            ) : null}
          </div>
        </div>

        {success ? (
          <div className="payment-success" aria-live="polite">
            <div className="payment-success-mark">✓</div>
            <div className="payment-success-text">支付成功</div>
          </div>
        ) : (
          <div className="muted" style={{ marginTop: "0.5rem", fontSize: "0.8em", textAlign: "center" }}>
            支付遇到问题？请联系客服：3193773138@qq.com
          </div>
        )}
      </div>
    </div>
  );
}

function PointsPaywallModal({
  open,
  reason,
  apiFetch,
  onClose,
  onPaid,
  pushToast,
}: {
  open: boolean;
  reason: string;
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  onClose: () => void;
  onPaid: () => void;
  pushToast: (toast: Toast) => void;
}) {
  const [loading, setLoading] = useState(false);
  const [plans, setPlans] = useState<BillingPlan[]>(() => cloneBillingFallbackPlans());
  const [points, setPoints] = useState<BillingPointsSnapshot | null>(null);
  const [checkoutBusy, setCheckoutBusy] = useState<string | null>(null);
  const [paymentState, setPaymentState] = useState<{
    open: boolean;
    planName: string;
    planCode: string;
    checkoutUrl: string;
    externalOrderId: string;
    amountText: string;
  }>({ open: false, planName: "", planCode: "", checkoutUrl: "", externalOrderId: "", amountText: "" });

  const refresh = useCallback(async () => {
    if (!open || loading) return;
    setLoading(true);
    try {
      const [plansResp, pointsResp] = await Promise.all([apiFetch("/billing/plans"), apiFetch("/billing/points/me")]);
      if (!plansResp.ok) {
        const text = await plansResp.text();
        throw new Error(text || `load plans failed (${plansResp.status})`);
      }
      const planRows = (await plansResp.json()) as BillingPlan[];
      setPlans(normalizePlansOrFallback(Array.isArray(planRows) ? planRows : []));
      if (pointsResp.ok) {
        setPoints((await pointsResp.json()) as BillingPointsSnapshot);
      } else {
        setPoints(null);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setPlans((prev) => (Array.isArray(prev) && prev.length > 0 ? prev : cloneBillingFallbackPlans()));
      pushToast({ type: "error", message: "充值面板加载失败", detail: `${message}。请刷新页面重试。` });
    } finally {
      setLoading(false);
    }
  }, [apiFetch, loading, open, pushToast]);

  useEffect(() => {
    if (!open) {
      setPaymentState({ open: false, planName: "", planCode: "", checkoutUrl: "", externalOrderId: "", amountText: "" });
      setCheckoutBusy(null);
      return;
    }
    refresh();
  }, [open, refresh]);

  const activePlans = useMemo(() => {
    const purchasable = plans.filter((item) => item.active);
    if (purchasable.length === 0) {
      return cloneBillingFallbackPlans();
    }
    return purchasable.sort((a, b) => a.price_cents - b.price_cents || a.name.localeCompare(b.name));
  }, [plans]);

  const startCheckout = useCallback(
    async (plan: BillingPlan) => {
      if (checkoutBusy) return;
      setCheckoutBusy(plan.plan_id);
      try {
        const response = await apiFetch("/billing/checkout", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            plan_code: plan.code,
            idempotency_key: `paywall:${plan.code}:${Date.now()}`,
          }),
        });
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || `checkout failed (${response.status})`);
        }
        const data = (await response.json()) as BillingCheckoutResponse;
        const url = String(data.checkout_url || "").trim();
        const externalOrderId = String(data.external_order_id || "").trim();
        if (!url || !externalOrderId) {
          throw new Error("后端未返回可用的 checkout_url/external_order_id");
        }
        setPaymentState({
          open: true,
          planName: plan.name,
          planCode: plan.code,
          checkoutUrl: url,
          externalOrderId,
          amountText: compactPlanPrice(plan.price_cents, plan.currency),
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        pushToast({ type: "error", message: "发起充值失败", detail: `${message}。如需帮助请联系客服 3193773138@qq.com` });
      } finally {
        setCheckoutBusy(null);
      }
    },
    [apiFetch, checkoutBusy, pushToast]
  );

  const handlePaid = useCallback(async () => {
    setPaymentState((prev) => ({ ...prev, open: false }));
    await refresh();
    onPaid();
    onClose();
  }, [onClose, onPaid, refresh]);

  if (!open) return null;

  return (
    <>
      <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Points Paywall">
        <div className="card modal-card paywall-card">
          <div className="modal-head">
            <div>
              <div className="section-title">余额不足</div>
              <div className="muted">{reason || "当前操作需要更多积分，请先充值后继续。"}</div>
            </div>
            <button className="ghost" type="button" onClick={onClose}>
              稍后再说
            </button>
          </div>

          <div className="paywall-balance">
            <span className="muted">当前积分</span>
            <span className="mono">{points ? formatNumber(points.balance) : "-"}</span>
            <button className="ghost" type="button" onClick={refresh} disabled={loading}>
              {loading ? "刷新中…" : "刷新余额"}
            </button>
          </div>

          <div className="billing-grid paywall-plan-grid">
            {activePlans.length ? (
              activePlans.map((plan) => (
                <div className="plan-card plan-card-dark" key={plan.plan_id}>
                  <div className="plan-head">
                    <div>
                      <div className="plan-title">{plan.name}</div>
                      <div className="plan-tagline">支付后即时到账</div>
                    </div>
                    <span className="pill pill-muted">{planIntervalLabel(plan.code)}</span>
                  </div>
                  <div className="plan-price-line">
                    <span className="plan-price">{compactPlanPrice(plan.price_cents, plan.currency)}</span>
                  </div>
                  <div className="plan-feature-list">
                    <div className="plan-feature-item">
                      <span className="plan-feature-dot">✦</span>
                      <span>购买即获 {formatNumber(plan.monthly_points)} 积分</span>
                    </div>
                  </div>
                  <div className="plan-actions">
                    <button
                      className="primary plan-cta"
                      type="button"
                      onClick={() => startCheckout(plan)}
                      disabled={Boolean(checkoutBusy)}
                    >
                      {checkoutBusy === plan.plan_id ? "生成二维码…" : "立即充值"}
                    </button>
                  </div>
                </div>
              ))
            ) : (
              <div className="muted">暂无可购买套餐，请联系管理员上架套餐。</div>
            )}
          </div>
        </div>
      </div>

      <PaymentModal
        open={paymentState.open}
        planName={paymentState.planName}
        planCode={paymentState.planCode}
        checkoutUrl={paymentState.checkoutUrl}
        externalOrderId={paymentState.externalOrderId}
        amountText={paymentState.amountText}
        apiFetch={apiFetch}
        onClose={() => setPaymentState((prev) => ({ ...prev, open: false }))}
        onPaid={handlePaid}
        pushToast={pushToast}
      />
    </>
  );
}

function AdminBillingPage({
  apiFetch,
  role,
  pushToast,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  role: string;
  pushToast: (toast: Toast) => void;
}) {
  const [tab, setTab] = useState<"plans" | "users" | "orders" | "audit">("plans");
  const [plans, setPlans] = useState<BillingPlan[]>([]);
  const [userBilling, setUserBilling] = useState<AdminUserBillingStatus[]>([]);
  const [orders, setOrders] = useState<BillingOrder[]>([]);
  const [audit, setAudit] = useState<BillingAuditLog[]>([]);
  const [auditDetail, setAuditDetail] = useState<BillingAuditLogDetail | null>(null);
  const [saasAdminEnabled, setSaasAdminEnabled] = useState(true);
  const [saasHint, setSaasHint] = useState("");
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [planEntitlements, setPlanEntitlements] = useState<BillingPlanEntitlement[]>([]);
  const [entitlementLimitDrafts, setEntitlementLimitDrafts] = useState<Record<string, string>>({});
  const [entitlementForm, setEntitlementForm] = useState({
    key: "",
    enabled: true,
    limit: "",
    valueJson: "{}",
    metadataJson: "{}",
  });
  const [entitlementBusy, setEntitlementBusy] = useState(false);
  const [bindForm, setBindForm] = useState({
    username: "",
    planId: "",
    durationDays: "",
    autoRenew: false,
  });
  const [bindBusy, setBindBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savingPlanId, setSavingPlanId] = useState("");
  const [planPriceDrafts, setPlanPriceDrafts] = useState<Record<string, string>>({});
  const [planPointsDrafts, setPlanPointsDrafts] = useState<Record<string, string>>({});
  const [userFilter, setUserFilter] = useState({ username: "", include_inactive: false });
  const [auditFilter, setAuditFilter] = useState({ provider: "", external_order_id: "", outcome: "" });

  const isAdmin = hasAdminAccess(role);

  const fetchJson = useCallback(
    async <T,>(path: string) => {
      const response = await apiFetch(path);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `request failed (${response.status})`);
      }
      return (await response.json()) as T;
    },
    [apiFetch]
  );

  const parseErrorText = useCallback(async (response: Response): Promise<string> => {
    try {
      const text = (await response.text()).trim();
      if (text) return text;
    } catch {
      // ignore
    }
    return `request failed (${response.status})`;
  }, []);

  const loadAdminPlans = useCallback(async (): Promise<BillingPlan[]> => {
    const saasResp = await apiFetch("/admin/saas/plans");
    if (saasResp.ok) {
      setSaasAdminEnabled(true);
      setSaasHint("");
      const rows = (await saasResp.json()) as BillingPlan[];
      return Array.isArray(rows) ? rows : [];
    }

    const detail = await parseErrorText(saasResp);
    const lower = detail.toLowerCase();
    const featureDisabled =
      saasResp.status === 404 &&
      (lower.includes("feature disabled") || lower.includes("feature_disabled") || detail.includes("功能未开启"));

    if (!featureDisabled) {
      throw new Error(detail || `load /admin/saas/plans failed (${saasResp.status})`);
    }

    setSaasAdminEnabled(false);
    setSaasHint("SaaS 管理开关未开启，当前回退到基础套餐视图。");
    const fallbackResp = await apiFetch("/billing/plans");
    if (!fallbackResp.ok) {
      const fallbackText = await parseErrorText(fallbackResp);
      throw new Error(fallbackText || `load /billing/plans failed (${fallbackResp.status})`);
    }
    const rows = (await fallbackResp.json()) as BillingPlan[];
    return Array.isArray(rows) ? rows : [];
  }, [apiFetch, parseErrorText]);

  const refreshPlanEntitlements = useCallback(
    async (planId: string) => {
      if (!saasAdminEnabled || !planId) {
        setPlanEntitlements([]);
        return;
      }
      try {
        const rows = await fetchJson<BillingPlanEntitlement[]>(
          `/admin/saas/plans/${encodeURIComponent(planId)}/entitlements?include_disabled=true`
        );
        setPlanEntitlements(Array.isArray(rows) ? rows : []);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        pushToast({ type: "error", message: "套餐权益加载失败", detail: message });
      }
    },
    [fetchJson, pushToast, saasAdminEnabled]
  );

  const refresh = useCallback(async () => {
    if (!isAdmin || busy) return;
    setBusy(true);
    try {
      if (tab === "plans") {
        const rows = await loadAdminPlans();
        const normalized = Array.isArray(rows) ? rows : [];
        setPlans(normalized);
        const nextPlanId =
          normalized.find((item) => item.plan_id === selectedPlanId)?.plan_id || normalized[0]?.plan_id || "";
        setSelectedPlanId(nextPlanId);
        if (!bindForm.planId && nextPlanId) {
          setBindForm((prev) => ({ ...prev, planId: nextPlanId }));
        }
        if (saasAdminEnabled && nextPlanId) {
          await refreshPlanEntitlements(nextPlanId);
        } else {
          setPlanEntitlements([]);
        }
      } else if (tab === "users") {
        if (!plans.length) {
          const rows = await loadAdminPlans();
          const normalized = Array.isArray(rows) ? rows : [];
          setPlans(normalized);
          if (!bindForm.planId && normalized.length) {
            setBindForm((prev) => ({ ...prev, planId: normalized[0].plan_id }));
          }
        }
        const query = new URLSearchParams();
        query.set("limit", "100");
        if (userFilter.username.trim()) query.set("username", userFilter.username.trim());
        if (userFilter.include_inactive) query.set("include_inactive", "true");
        const data = await fetchJson<AdminUserBillingStatus[]>(`/admin/billing/users/status?${query.toString()}`);
        setUserBilling(Array.isArray(data) ? data : []);
      } else if (tab === "orders") {
        const data = await fetchJson<BillingOrder[]>("/admin/billing/orders?limit=50");
        setOrders(Array.isArray(data) ? data : []);
      } else {
        const query = new URLSearchParams();
        query.set("limit", "50");
        if (auditFilter.provider.trim()) query.set("provider", auditFilter.provider.trim());
        if (auditFilter.external_order_id.trim()) query.set("external_order_id", auditFilter.external_order_id.trim());
        if (auditFilter.outcome.trim()) query.set("outcome", auditFilter.outcome.trim());
        const data = await fetchJson<BillingAuditLog[]>(`/admin/billing/audit?${query.toString()}`);
        setAudit(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "管理数据加载失败", detail: message });
    } finally {
      setBusy(false);
    }
  }, [
    auditFilter.external_order_id,
    auditFilter.outcome,
    auditFilter.provider,
    busy,
    fetchJson,
    isAdmin,
    loadAdminPlans,
    pushToast,
    refreshPlanEntitlements,
    saasAdminEnabled,
    selectedPlanId,
    tab,
    bindForm.planId,
    plans.length,
    userFilter.include_inactive,
    userFilter.username,
  ]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    setPlanPriceDrafts((prev) => {
      const next: Record<string, string> = {};
      plans.forEach((plan) => {
        const existing = prev[plan.plan_id];
        next[plan.plan_id] = existing ?? String(plan.price_cents);
      });
      return next;
    });
    setPlanPointsDrafts((prev) => {
      const next: Record<string, string> = {};
      plans.forEach((plan) => {
        const existing = prev[plan.plan_id];
        next[plan.plan_id] = existing ?? String(plan.monthly_points);
      });
      return next;
    });
  }, [plans]);

  useEffect(() => {
    if (!plans.length) {
      setSelectedPlanId("");
      setBindForm((prev) => ({ ...prev, planId: "" }));
      return;
    }
    setSelectedPlanId((prev) => (prev && plans.some((item) => item.plan_id === prev) ? prev : plans[0].plan_id));
    setBindForm((prev) => {
      if (prev.planId && plans.some((item) => item.plan_id === prev.planId)) {
        return prev;
      }
      return { ...prev, planId: plans[0].plan_id };
    });
  }, [plans]);

  useEffect(() => {
    setEntitlementLimitDrafts((prev) => {
      const next: Record<string, string> = {};
      planEntitlements.forEach((item) => {
        const existing = prev[item.entitlement_id];
        next[item.entitlement_id] = existing ?? (item.limit == null ? "" : String(item.limit));
      });
      return next;
    });
  }, [planEntitlements]);

  useEffect(() => {
    if (tab !== "plans" || !saasAdminEnabled || !selectedPlanId) return;
    void refreshPlanEntitlements(selectedPlanId);
  }, [refreshPlanEntitlements, saasAdminEnabled, selectedPlanId, tab]);

  const updatePlan = async (
    plan: BillingPlan,
    patch: Partial<BillingPlan>,
    successMessage = "套餐已更新"
  ) => {
    if (!isAdmin) return;
    try {
      const endpoint = saasAdminEnabled
        ? `/admin/saas/plans/${encodeURIComponent(plan.plan_id)}`
        : `/admin/billing/plans/${encodeURIComponent(plan.plan_id)}`;
      const response = await apiFetch(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: patch.code ?? undefined,
          name: patch.name ?? undefined,
          description: patch.description ?? undefined,
          currency: patch.currency ?? undefined,
          price_cents: patch.price_cents ?? undefined,
          monthly_points: patch.monthly_points ?? undefined,
          billing_cycle: patch.billing_cycle ?? undefined,
          trial_days: patch.trial_days ?? undefined,
          metadata: patch.metadata ?? undefined,
          active: patch.active ?? undefined,
        }),
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `update failed (${response.status})`);
      }
      pushToast({ type: "success", message: successMessage });
      refresh();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "套餐更新失败", detail: message });
    }
  };

  const savePlanPrice = async (plan: BillingPlan) => {
    const raw = (planPriceDrafts[plan.plan_id] ?? String(plan.price_cents)).trim();
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
      pushToast({ type: "error", message: "价格无效", detail: "price_cents 必须是大于等于 0 的整数" });
      return;
    }
    setSavingPlanId(plan.plan_id);
    await updatePlan(plan, { price_cents: parsed }, "价格已更新");
    setSavingPlanId("");
  };

  const savePlanPoints = async (plan: BillingPlan) => {
    const raw = (planPointsDrafts[plan.plan_id] ?? String(plan.monthly_points)).trim();
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
      pushToast({ type: "error", message: "积分无效", detail: "monthly_points 必须是大于等于 0 的整数" });
      return;
    }
    setSavingPlanId(plan.plan_id);
    await updatePlan(plan, { monthly_points: parsed }, "积分额度已更新");
    setSavingPlanId("");
  };

  const parseOptionalJson = (raw: string, label: string, fallback: unknown) => {
    const text = String(raw || "").trim();
    if (!text) return fallback;
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`${label} 不是合法 JSON`);
    }
  };

  const createEntitlement = async () => {
    if (!saasAdminEnabled) {
      pushToast({ type: "error", message: "SaaS 管理未开启", detail: saasHint || "请先开启 FEATURE_SAAS_ADMIN_API" });
      return;
    }
    if (!selectedPlanId) {
      pushToast({ type: "error", message: "请先选择套餐" });
      return;
    }
    const key = entitlementForm.key.trim().toLowerCase();
    if (!key) {
      pushToast({ type: "error", message: "权益 key 不能为空" });
      return;
    }
    let limitValue: number | null = null;
    const limitText = entitlementForm.limit.trim();
    if (limitText) {
      const parsed = Number.parseInt(limitText, 10);
      if (!Number.isFinite(parsed) || parsed < 0) {
        pushToast({ type: "error", message: "limit 无效", detail: "limit 必须是大于等于 0 的整数" });
        return;
      }
      limitValue = parsed;
    }

    try {
      const valuePayload = parseOptionalJson(entitlementForm.valueJson, "value", null);
      const metadataPayload = parseOptionalJson(entitlementForm.metadataJson, "metadata", {});
      setEntitlementBusy(true);
      const response = await apiFetch(`/admin/saas/plans/${encodeURIComponent(selectedPlanId)}/entitlements`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key,
          enabled: entitlementForm.enabled,
          limit: limitValue,
          value: valuePayload,
          metadata: metadataPayload,
        }),
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `create entitlement failed (${response.status})`);
      }
      setEntitlementForm({
        key: "",
        enabled: true,
        limit: "",
        valueJson: "{}",
        metadataJson: "{}",
      });
      pushToast({ type: "success", message: "权益已创建" });
      await refreshPlanEntitlements(selectedPlanId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "创建权益失败", detail: message });
    } finally {
      setEntitlementBusy(false);
    }
  };

  const saveEntitlementLimit = async (item: BillingPlanEntitlement) => {
    if (!saasAdminEnabled) return;
    const draft = (entitlementLimitDrafts[item.entitlement_id] ?? "").trim();
    let limitValue: number | null = null;
    if (draft) {
      const parsed = Number.parseInt(draft, 10);
      if (!Number.isFinite(parsed) || parsed < 0) {
        pushToast({ type: "error", message: "limit 无效", detail: "limit 必须是大于等于 0 的整数" });
        return;
      }
      limitValue = parsed;
    }
    try {
      setEntitlementBusy(true);
      const response = await apiFetch(`/admin/saas/entitlements/${encodeURIComponent(item.entitlement_id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: limitValue }),
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `update entitlement failed (${response.status})`);
      }
      pushToast({ type: "success", message: "权益 limit 已更新" });
      await refreshPlanEntitlements(selectedPlanId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "更新权益失败", detail: message });
    } finally {
      setEntitlementBusy(false);
    }
  };

  const toggleEntitlement = async (item: BillingPlanEntitlement) => {
    if (!saasAdminEnabled) return;
    try {
      setEntitlementBusy(true);
      const response = await apiFetch(`/admin/saas/entitlements/${encodeURIComponent(item.entitlement_id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !item.enabled }),
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `toggle entitlement failed (${response.status})`);
      }
      pushToast({ type: "success", message: item.enabled ? "权益已禁用" : "权益已启用" });
      await refreshPlanEntitlements(selectedPlanId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "更新权益失败", detail: message });
    } finally {
      setEntitlementBusy(false);
    }
  };

  const deleteEntitlement = async (item: BillingPlanEntitlement) => {
    if (!saasAdminEnabled) return;
    try {
      setEntitlementBusy(true);
      const response = await apiFetch(`/admin/saas/entitlements/${encodeURIComponent(item.entitlement_id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `delete entitlement failed (${response.status})`);
      }
      pushToast({ type: "success", message: "权益已删除" });
      await refreshPlanEntitlements(selectedPlanId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "删除权益失败", detail: message });
    } finally {
      setEntitlementBusy(false);
    }
  };

  const bindUserPlan = async () => {
    if (!saasAdminEnabled) {
      pushToast({ type: "error", message: "SaaS 管理未开启", detail: saasHint || "请先开启 FEATURE_SAAS_ADMIN_API" });
      return;
    }
    const username = bindForm.username.trim();
    if (!username) {
      pushToast({ type: "error", message: "用户名不能为空" });
      return;
    }
    const planId = bindForm.planId || selectedPlanId;
    if (!planId) {
      pushToast({ type: "error", message: "请选择套餐" });
      return;
    }
    let durationDays: number | undefined;
    const rawDuration = bindForm.durationDays.trim();
    if (rawDuration) {
      const parsed = Number.parseInt(rawDuration, 10);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        pushToast({ type: "error", message: "duration_days 无效", detail: "必须是正整数" });
        return;
      }
      durationDays = parsed;
    }
    try {
      setBindBusy(true);
      const payload: Record<string, unknown> = {
        plan_id: planId,
        auto_renew: bindForm.autoRenew,
      };
      if (durationDays !== undefined) payload.duration_days = durationDays;
      const response = await apiFetch(`/admin/saas/users/${encodeURIComponent(username)}/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const text = await parseErrorText(response);
        throw new Error(text || `bind plan failed (${response.status})`);
      }
      const data = (await response.json()) as BillingSubscriptionSnapshot;
      pushToast({
        type: "success",
        message: "用户套餐绑定成功",
        detail: `${username} -> ${data.plan_name || data.plan_code || planId}`,
      });
      setBindForm((prev) => ({ ...prev, username: "", durationDays: "" }));
      if (tab === "users") {
        await refresh();
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "绑定套餐失败", detail: message });
    } finally {
      setBindBusy(false);
    }
  };

  const openAuditDetail = async (logId: string) => {
    if (!isAdmin) return;
    try {
      const data = await fetchJson<BillingAuditLogDetail>(`/admin/billing/audit/${encodeURIComponent(logId)}`);
      setAuditDetail(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      pushToast({ type: "error", message: "审计日志加载失败", detail: message });
    }
  };

  if (!isAdmin) {
    return (
      <div className="create-grid" data-testid="page-admin-billing">
        <div className="card">
          <div className="section-title">管理端</div>
          <div className="error-banner">需要管理员权限才能访问。</div>
        </div>
      </div>
    );
  }

  return (
    <div className="create-grid" data-testid="page-admin-billing">
      <div className="card card-hero">
        <div className="detail-head">
          <div className="detail-head-left">
            <div>
              <div className="section-title">Billing Admin</div>
              <div className="section-sub">套餐配置、权益管理、用户绑定、订单审计。</div>
            </div>
            <div className="row-actions">
              <span className={`pill ${saasAdminEnabled ? "pill-success" : "pill-warn"}`}>
                {saasAdminEnabled ? "SaaS 管理已开启" : "SaaS 管理未开启"}
              </span>
              <button className="ghost" type="button" onClick={refresh} disabled={busy}>
                {busy ? "刷新中…" : "刷新"}
              </button>
            </div>
          </div>
        </div>
        {!saasAdminEnabled && saasHint ? <div className="muted">{saasHint}</div> : null}
        <div className="tabs" role="tablist" aria-label="Billing admin tabs">
          {(
            [
              { id: "plans", label: "套餐" },
              { id: "users", label: "订阅查询" },
              { id: "orders", label: "订单" },
              { id: "audit", label: "审计" },
            ] as const
          ).map((item) => (
            <button
              key={item.id}
              className={`tab ${tab === item.id ? "active" : ""}`}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              onClick={() => {
                setAuditDetail(null);
                setTab(item.id);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "plans" ? (
        <div className="card table-card">
          <div className="table billing-admin-table billing-plan-table">
            <div className="table-header">
              <div className="cell">Code</div>
              <div className="cell">Name</div>
              <div className="cell">Cycle</div>
              <div className="cell">Trial</div>
              <div className="cell">Price</div>
              <div className="cell">Points</div>
              <div className="cell">Active</div>
              <div className="cell">Actions</div>
            </div>
            {plans.map((plan) => (
              <div className="table-row" key={plan.plan_id}>
                <div className="cell mono">{plan.code}</div>
                <div className="cell">{plan.name}</div>
                <div className="cell mono">{plan.billing_cycle || "-"}</div>
                <div className="cell mono">{plan.trial_days == null ? "-" : plan.trial_days}</div>
                <div className="cell mono">{plan.price_cents}</div>
                <div className="cell mono">{plan.monthly_points}</div>
                <div className="cell">{plan.active ? <span className="pill pill-success">Yes</span> : <span className="pill pill-danger">No</span>}</div>
                <div className="cell row-actions">
                  <input
                    className="admin-inline-input mono"
                    type="number"
                    min={0}
                    step={1}
                    value={planPriceDrafts[plan.plan_id] ?? String(plan.price_cents)}
                    onChange={(event) =>
                      setPlanPriceDrafts((prev) => ({
                        ...prev,
                        [plan.plan_id]: event.target.value,
                      }))
                    }
                    aria-label={`price-${plan.code}`}
                  />
                  <button
                    className="ghost"
                    type="button"
                    onClick={() => savePlanPrice(plan)}
                    disabled={savingPlanId === plan.plan_id}
                  >
                    {savingPlanId === plan.plan_id ? "保存中…" : "保存价格"}
                  </button>
                  <input
                    className="admin-inline-input mono"
                    type="number"
                    min={0}
                    step={1}
                    value={planPointsDrafts[plan.plan_id] ?? String(plan.monthly_points)}
                    onChange={(event) =>
                      setPlanPointsDrafts((prev) => ({
                        ...prev,
                        [plan.plan_id]: event.target.value,
                      }))
                    }
                    aria-label={`points-${plan.code}`}
                  />
                  <button
                    className="ghost"
                    type="button"
                    onClick={() => savePlanPoints(plan)}
                    disabled={savingPlanId === plan.plan_id}
                  >
                    {savingPlanId === plan.plan_id ? "保存中…" : "保存积分"}
                  </button>
                  <button
                    className="ghost"
                    type="button"
                    onClick={() => updatePlan(plan, { active: !plan.active }, plan.active ? "套餐已下架" : "套餐已上架")}
                  >
                    {plan.active ? "下架" : "上架"}
                  </button>
                </div>
              </div>
            ))}
            {!plans.length ? <div className="muted">暂无套餐</div> : null}
          </div>
          {saasAdminEnabled ? (
            <>
              <div className="recommend-section-head">
                <div>
                  <div className="section-title">套餐权益管理</div>
                  <div className="muted">查看并调整当前套餐的 entitlement key/limit/enabled。</div>
                </div>
              </div>
              <div className="filters-panel">
                <label className="field">
                  <span>套餐</span>
                  <select
                    value={selectedPlanId}
                    onChange={(event) => setSelectedPlanId(event.target.value)}
                    disabled={!plans.length}
                  >
                    {plans.map((item) => (
                      <option key={item.plan_id} value={item.plan_id}>
                        {item.name} ({item.code})
                      </option>
                    ))}
                  </select>
                </label>
                <div className="row-actions">
                  <button
                    className="ghost"
                    type="button"
                    onClick={() => {
                      if (!selectedPlanId) return;
                      void refreshPlanEntitlements(selectedPlanId);
                    }}
                    disabled={!selectedPlanId || entitlementBusy}
                  >
                    刷新权益
                  </button>
                </div>
              </div>
              <div className="filters-panel">
                <label className="field">
                  <span>Key</span>
                  <input
                    value={entitlementForm.key}
                    onChange={(event) =>
                      setEntitlementForm((prev) => ({ ...prev, key: event.target.value }))
                    }
                    placeholder="feature.deep_search"
                  />
                </label>
                <label className="field">
                  <span>Limit</span>
                  <input
                    value={entitlementForm.limit}
                    onChange={(event) =>
                      setEntitlementForm((prev) => ({ ...prev, limit: event.target.value }))
                    }
                    placeholder="300"
                  />
                </label>
                <label className="field field-checkbox">
                  <span>Enabled</span>
                  <input
                    type="checkbox"
                    checked={entitlementForm.enabled}
                    onChange={(event) =>
                      setEntitlementForm((prev) => ({ ...prev, enabled: event.target.checked }))
                    }
                  />
                </label>
                <label className="field">
                  <span>Value JSON</span>
                  <textarea
                    rows={2}
                    value={entitlementForm.valueJson}
                    onChange={(event) =>
                      setEntitlementForm((prev) => ({ ...prev, valueJson: event.target.value }))
                    }
                  />
                </label>
                <label className="field">
                  <span>Metadata JSON</span>
                  <textarea
                    rows={2}
                    value={entitlementForm.metadataJson}
                    onChange={(event) =>
                      setEntitlementForm((prev) => ({ ...prev, metadataJson: event.target.value }))
                    }
                  />
                </label>
                <div className="row-actions">
                  <button
                    className="primary"
                    type="button"
                    onClick={createEntitlement}
                    disabled={!selectedPlanId || entitlementBusy}
                  >
                    {entitlementBusy ? "提交中…" : "新增权益"}
                  </button>
                </div>
              </div>
              <div className="table billing-admin-table billing-entitlement-table">
                <div className="table-header">
                  <div className="cell">Key</div>
                  <div className="cell">Enabled</div>
                  <div className="cell">Limit</div>
                  <div className="cell">Value</div>
                  <div className="cell">Metadata</div>
                  <div className="cell">Actions</div>
                </div>
                {planEntitlements.map((item) => (
                  <div className="table-row" key={item.entitlement_id}>
                    <div className="cell mono">{item.key}</div>
                    <div className="cell">
                      <span className={`pill ${item.enabled ? "pill-success" : "pill-danger"}`}>
                        {item.enabled ? "enabled" : "disabled"}
                      </span>
                    </div>
                    <div className="cell row-actions">
                      <input
                        className="admin-inline-input mono"
                        value={entitlementLimitDrafts[item.entitlement_id] ?? ""}
                        onChange={(event) =>
                          setEntitlementLimitDrafts((prev) => ({
                            ...prev,
                            [item.entitlement_id]: event.target.value,
                          }))
                        }
                        placeholder="-"
                      />
                      <button className="ghost" type="button" onClick={() => saveEntitlementLimit(item)} disabled={entitlementBusy}>
                        保存
                      </button>
                    </div>
                    <div className="cell mono">
                      <pre className="code-block">{JSON.stringify(item.value ?? null, null, 2)}</pre>
                    </div>
                    <div className="cell mono">
                      <pre className="code-block">{JSON.stringify(item.metadata ?? {}, null, 2)}</pre>
                    </div>
                    <div className="cell row-actions">
                      <button className="ghost" type="button" onClick={() => toggleEntitlement(item)} disabled={entitlementBusy}>
                        {item.enabled ? "禁用" : "启用"}
                      </button>
                      <button className="ghost" type="button" onClick={() => deleteEntitlement(item)} disabled={entitlementBusy}>
                        删除
                      </button>
                    </div>
                  </div>
                ))}
                {!planEntitlements.length ? <div className="muted">当前套餐暂无权益配置</div> : null}
              </div>
            </>
          ) : null}
        </div>
      ) : null}

      {tab === "users" ? (
        <>
          {saasAdminEnabled ? (
            <div className="card">
              <div className="section-title">手动绑定套餐</div>
              <div className="filters-panel">
                <label className="field">
                  <span>用户名</span>
                  <input
                    value={bindForm.username}
                    onChange={(event) => setBindForm((prev) => ({ ...prev, username: event.target.value }))}
                    placeholder="alice"
                  />
                </label>
                <label className="field">
                  <span>套餐</span>
                  <select
                    value={bindForm.planId}
                    onChange={(event) => setBindForm((prev) => ({ ...prev, planId: event.target.value }))}
                    disabled={!plans.length}
                  >
                    {plans.map((item) => (
                      <option key={item.plan_id} value={item.plan_id}>
                        {item.name} ({item.code})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Duration Days（可空）</span>
                  <input
                    value={bindForm.durationDays}
                    onChange={(event) => setBindForm((prev) => ({ ...prev, durationDays: event.target.value }))}
                    placeholder="30"
                  />
                </label>
                <label className="field field-checkbox">
                  <span>Auto Renew</span>
                  <input
                    type="checkbox"
                    checked={bindForm.autoRenew}
                    onChange={(event) => setBindForm((prev) => ({ ...prev, autoRenew: event.target.checked }))}
                  />
                </label>
                <div className="row-actions">
                  <button className="primary" type="button" onClick={bindUserPlan} disabled={bindBusy || !plans.length}>
                    {bindBusy ? "绑定中…" : "绑定套餐"}
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="card">
              <div className="muted">{saasHint || "SaaS 管理开关未开启，当前仅支持订阅查询。"} </div>
            </div>
          )}
          <div className="card">
            <div className="filters-panel">
              <label className="field">
                <span>用户名（精确）</span>
                <input
                  value={userFilter.username}
                  onChange={(e) => setUserFilter((prev) => ({ ...prev, username: e.target.value }))}
                  placeholder="alice"
                />
              </label>
              <label className="field field-checkbox">
                <span>包含停用账号</span>
                <input
                  type="checkbox"
                  checked={userFilter.include_inactive}
                  onChange={(e) => setUserFilter((prev) => ({ ...prev, include_inactive: e.target.checked }))}
                />
              </label>
              <div className="row-actions">
                <button className="primary" type="button" onClick={refresh} disabled={busy}>
                  查询
                </button>
              </div>
            </div>
          </div>
          <div className="card table-card">
            <div className="table billing-admin-table billing-user-table">
              <div className="table-header">
                <div className="cell">User</div>
                <div className="cell">Role</div>
                <div className="cell">Tenant</div>
                <div className="cell">Account</div>
                <div className="cell">Plan</div>
                <div className="cell">Expire At</div>
                <div className="cell">Points</div>
                <div className="cell">Sub Status</div>
              </div>
              {userBilling.map((item) => (
                <div className="table-row" key={item.username}>
                  <div className="cell mono">{item.username}</div>
                  <div className="cell mono">{item.role}</div>
                  <div className="cell">
                    <div>{item.tenant_name || "-"}</div>
                    <div className="muted mono">{item.tenant_code || item.tenant_id || "-"}</div>
                  </div>
                  <div className="cell">
                    {item.active ? <span className="pill pill-success">active</span> : <span className="pill pill-danger">disabled</span>}
                  </div>
                  <div className="cell mono">{item.subscription.plan_name || item.subscription.plan_code || "-"}</div>
                  <div className="cell mono">{item.subscription.expires_at || "-"}</div>
                  <div className="cell mono">{formatNumber(item.points_balance)}</div>
                  <div className="cell">
                    <span className={`pill ${String(item.subscription.status).toLowerCase() === "active" ? "pill-success" : "pill-muted"}`}>
                      {item.subscription.status || "none"}
                    </span>
                  </div>
                </div>
              ))}
              {!userBilling.length ? <div className="muted">暂无匹配用户</div> : null}
            </div>
          </div>
        </>
      ) : null}

      {tab === "orders" ? (
        <div className="card table-card">
          <div className="table billing-admin-table billing-order-table">
            <div className="table-header">
              <div className="cell">Created</div>
              <div className="cell">User</div>
              <div className="cell">Plan</div>
              <div className="cell">Amount</div>
              <div className="cell">Status</div>
              <div className="cell">External</div>
            </div>
            {orders.map((order) => (
              <div className="table-row" key={order.order_id}>
                <div className="cell mono">{order.created_at || "-"}</div>
                <div className="cell mono">{order.user_id}</div>
                <div className="cell mono">{order.plan_code || order.plan_id}</div>
                <div className="cell mono">
                  {order.amount_cents} {(order.currency || "").toUpperCase()}
                </div>
                <div className="cell">
                  <span className={`pill ${String(order.status).toLowerCase() === "paid" ? "pill-success" : "pill-muted"}`}>
                    {order.status}
                  </span>
                </div>
                <div className="cell mono">{order.external_order_id || "-"}</div>
              </div>
            ))}
            {!orders.length ? <div className="muted">暂无订单</div> : null}
          </div>
        </div>
      ) : null}

      {tab === "audit" ? (
        <>
          <div className="card">
            <div className="filters-panel">
              <label className="field">
                <span>Provider</span>
                <input
                  value={auditFilter.provider}
                  onChange={(e) => setAuditFilter((prev) => ({ ...prev, provider: e.target.value }))}
                  placeholder="wechatpay/internal/dev"
                />
              </label>
              <label className="field">
                <span>External Order</span>
                <input
                  value={auditFilter.external_order_id}
                  onChange={(e) => setAuditFilter((prev) => ({ ...prev, external_order_id: e.target.value }))}
                  placeholder="ord_ext_..."
                />
              </label>
              <label className="field">
                <span>Outcome</span>
                <input
                  value={auditFilter.outcome}
                  onChange={(e) => setAuditFilter((prev) => ({ ...prev, outcome: e.target.value }))}
                  placeholder="processed|rejected_signature|error"
                />
              </label>
              <div className="row-actions">
                <button className="primary" type="button" onClick={refresh} disabled={busy}>
                  查询
                </button>
              </div>
            </div>
          </div>
          <div className="card table-card">
            <div className="table billing-admin-table billing-audit-table">
              <div className="table-header">
                <div className="cell">Time</div>
                <div className="cell">Provider</div>
                <div className="cell">Event</div>
                <div className="cell">External</div>
                <div className="cell">Sig</div>
                <div className="cell">Outcome</div>
              </div>
              {audit.map((log) => (
                <div className="table-row is-clickable" key={log.log_id} onClick={() => openAuditDetail(log.log_id)}>
                  <div className="cell mono">{log.occurred_at}</div>
                  <div className="cell mono">{log.provider}</div>
                  <div className="cell mono">{log.event_type}</div>
                  <div className="cell mono">{log.external_order_id || "-"}</div>
                  <div className="cell">
                    {log.signature_valid ? <span className="pill pill-success">valid</span> : <span className="pill pill-danger">bad</span>}
                  </div>
                  <div className="cell mono">{log.outcome}</div>
                </div>
              ))}
              {!audit.length ? <div className="muted">暂无审计日志</div> : null}
            </div>
          </div>
          {auditDetail ? (
            <div className="card">
              <div className="recommend-section-head">
                <div>
                  <div className="section-title">审计详情</div>
                  <div className="muted mono">{auditDetail.log_id}</div>
                </div>
                <button className="ghost" type="button" onClick={() => setAuditDetail(null)}>
                  关闭
                </button>
              </div>
              <pre className="code-block">{JSON.stringify(auditDetail, null, 2)}</pre>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function App() {
  const { route, navigate } = useRoute();
  const [token, setToken] = useState<string | null>(() => safeStorageGet(AUTH_TOKEN_STORAGE_KEY));
  const [user, setUser] = useState<AuthUserInfo | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [paywallState, setPaywallState] = useState<{ open: boolean; reason: string }>({ open: false, reason: "" });

  const logout = useCallback(() => {
    safeStorageRemove(AUTH_TOKEN_STORAGE_KEY);
    setToken(null);
    setUser(null);
    setAuthStatus("unauthenticated");
  }, []);

  const apiFetch = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const url = /^https?:\/\//i.test(path) ? path : joinUrl(API_BASE, path);
      const normalizedPath = /^https?:\/\//i.test(path)
        ? (() => {
            try {
              return new URL(path).pathname;
            } catch {
              return "";
            }
          })()
        : String(path || "");
      const headers = new Headers(init.headers);
      if (
        token &&
        !headers.has("Authorization") &&
        !normalizedPath.startsWith("/auth/login") &&
        !normalizedPath.startsWith("/auth/register")
      ) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      const response = await fetch(url, { ...init, headers });
      if (response.status === 401 && !normalizedPath.startsWith("/auth/login") && !normalizedPath.startsWith("/auth/register")) {
        logout();
      }
      if (response.status === 402 && !normalizedPath.startsWith("/billing/")) {
        let detail = "";
        try {
          const payload = (await response.clone().json()) as { detail?: string; message?: string };
          detail = String(payload?.detail || payload?.message || "").trim();
        } catch {
          try {
            detail = String(await response.clone().text()).trim();
          } catch {
            detail = "";
          }
        }
        setPaywallState({
          open: true,
          reason: detail || "积分不足，请充值后继续。",
        });
      }
      return response;
    },
    [token, logout]
  );

  const buildWsUrl = useCallback(
    (path: string) => {
      const base = resolveWsBase(API_BASE);
      const url = /^wss?:\/\//i.test(path) ? path : joinUrl(base, path);
      if (!token) return url;
      const sep = url.includes("?") ? "&" : "?";
      return `${url}${sep}token=${encodeURIComponent(token)}`;
    },
    [token]
  );

  const login = useCallback(async (username: string, password: string) => {
    const response = await fetch(joinUrl(API_BASE, "/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) {
      const message = await parseAuthError(response, `登录失败 (${response.status})`);
      throw new Error(message);
    }
    const data = (await response.json()) as AuthLoginResponse;
    const nextToken = String(data.access_token || "").trim();
    if (!nextToken) {
      throw new Error("登录失败：缺少 access_token");
    }
    safeStorageSet(AUTH_TOKEN_STORAGE_KEY, nextToken);
    setToken(nextToken);
    setUser(data.user);
    setAuthStatus("authenticated");
  }, []);

  const register = useCallback(async (username: string, password: string, tenantName: string, tenantCode: string) => {
    const response = await fetch(joinUrl(API_BASE, "/auth/register"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        tenant_name: tenantName || undefined,
        tenant_code: tenantCode || undefined,
      }),
    });
    if (!response.ok) {
      const message = await parseAuthError(response, `注册失败 (${response.status})`);
      throw new Error(message);
    }
    const data = (await response.json()) as AuthLoginResponse;
    const nextToken = String(data.access_token || "").trim();
    if (!nextToken) {
      throw new Error("注册失败：缺少 access_token");
    }
    safeStorageSet(AUTH_TOKEN_STORAGE_KEY, nextToken);
    setToken(nextToken);
    setUser(data.user);
    setAuthStatus("authenticated");
  }, []);

  useEffect(() => {
    let cancelled = false;
    const bootstrap = async () => {
      setAuthStatus("checking");

      if (token) {
        try {
          const response = await apiFetch("/auth/me");
          if (!response.ok) {
            throw new Error(`auth/me ${response.status}`);
          }
          const me = (await response.json()) as AuthUserInfo;
          if (!cancelled) {
            setUser(me);
            setAuthStatus("authenticated");
          }
          return;
        } catch {
          if (!cancelled) {
            logout();
          }
          return;
        }
      }

      // If backend auth is disabled, protected endpoints should be reachable without a token.
      try {
        const probe = await fetch(joinUrl(API_BASE, "/case-templates"));
        if (cancelled) return;
        if (probe.ok) {
          setUser({ username: "anonymous", role: "user" });
          setAuthStatus("authenticated");
          return;
        }
        setAuthStatus("unauthenticated");
      } catch {
        if (!cancelled) {
          setAuthStatus("unauthenticated");
        }
      }
    };
    bootstrap();
    return () => {
      cancelled = true;
    };
  }, [token, apiFetch, logout]);

  const authContextValue = useMemo<AuthContextValue>(
    () => ({
      status: authStatus,
      token,
      user,
      login,
      register,
      logout,
      apiFetch,
      buildWsUrl,
    }),
    [authStatus, token, user, login, register, logout, apiFetch, buildWsUrl]
  );

  const [flashCaseId, setFlashCaseId] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [localDeployOpen, setLocalDeployOpen] = useState(false);

  useEffect(() => {
    mermaid.initialize({ startOnLoad: false, theme: "neutral" });
  }, []);

  useEffect(() => {
    return () => undefined;
  }, []);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = window.setTimeout(() => setToast(null), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const pushToast = useCallback((next: Toast) => {
    setToast(next);
  }, []);

  const closePaywall = useCallback(() => {
    setPaywallState((prev) => ({ ...prev, open: false }));
  }, []);

  const handlePaywallPaid = useCallback(() => {
    setPaywallState({ open: false, reason: "" });
    window.dispatchEvent(new CustomEvent("antihub:points-updated"));
    pushToast({ type: "success", message: "充值成功，积分已到账" });
  }, [pushToast]);

  useEffect(() => {
    if (authStatus !== "authenticated") return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.repeat) return;
      const target = event.target as HTMLElement | null;
      const tag = String(target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;

      if (event.key === "1") {
        event.preventDefault();
        navigate("/create");
        return;
      }
      if (event.key === "2") {
        event.preventDefault();
        navigate("/workspace");
        return;
      }
      if (event.key === "3") {
        event.preventDefault();
        navigate("/billing");
        return;
      }
      if (event.key === "4" && hasAdminAccess(user?.role || "")) {
        event.preventDefault();
        navigate("/admin/billing");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [authStatus, navigate, user?.role]);

  useEffect(() => {
    if (authStatus !== "authenticated") return;
    if (route.type === "admin_billing" && !hasAdminAccess(user?.role || "")) {
      pushToast({ type: "error", message: "无权限访问管理页", detail: "需要管理员角色。" });
      navigate("/workspace");
    }
  }, [authStatus, navigate, pushToast, route.type, user?.role]);

  const consoleActive = route.type === "create" || route.type === "case";
  const workspaceActive = route.type === "workspace";
  const billingActive = route.type === "billing";
  const adminBillingActive = route.type === "admin_billing";
  const primaryAction =
    route.type === "case"
      ? {
          label: "刷新状态",
          onClick: () => window.dispatchEvent(new CustomEvent("antihub:case-refresh")),
          testId: "btn-refresh-case",
        }
      : null;

  return (
    <AuthContext.Provider value={authContextValue}>
      <div className="app" data-testid="app-root">
        <header className="topbar">
          <div className="brand">
            <div className="brand-mark">AH</div>
            <div>
              <div className="brand-title">智能讲代码</div>
              <div className="brand-sub">一键看懂仓库</div>
            </div>
          </div>
          <nav className="global-nav" aria-label="Global navigation">
            <button
              className={`nav-item ${consoleActive ? "active" : ""}`}
              type="button"
              onClick={() => navigate("/create")}
            >
              <span className="nav-main">控制台</span>
              <span className="nav-sub">仓库讲解</span>
            </button>
            <button
              className={`nav-item ${workspaceActive ? "active" : ""}`}
              type="button"
              onClick={() => navigate("/workspace")}
            >
              <span className="nav-main">工作台</span>
              <span className="nav-sub">租户与权限</span>
            </button>
            <button
              className={`nav-item ${billingActive ? "active" : ""}`}
              type="button"
              onClick={() => navigate("/billing")}
            >
              <span className="nav-main">会员</span>
              <span className="nav-sub">套餐与积分</span>
            </button>
            {hasAdminAccess(user?.role || "") ? (
              <button
                className={`nav-item ${adminBillingActive ? "active" : ""}`}
                type="button"
                onClick={() => navigate("/admin/billing")}
              >
                <span className="nav-main">管理</span>
                <span className="nav-sub">定价与审计</span>
              </button>
            ) : null}
          </nav>
          <div className="topbar-user">
            {authStatus === "authenticated" && user ? (
              <>
                <span className="mono">{user.username}</span>
                {user.tenant_name ? <span className="pill pill-muted">{user.tenant_name}</span> : null}
                <span className="pill">{user.role}</span>
                <span className="hotkey-hint">Alt+1/2/3{hasAdminAccess(user.role) ? "/4" : ""}</span>
              </>
            ) : (
              <span className="muted">未登录</span>
            )}
          </div>
          <div className="topbar-actions">
            {primaryAction && authStatus === "authenticated" ? (
              <button
                className="primary"
                type="button"
                onClick={primaryAction.onClick}
                data-testid={primaryAction.testId}
              >
                {primaryAction.label}
              </button>
            ) : null}
            {authStatus === "authenticated" ? (
              <button className="ghost" type="button" onClick={logout}>
                退出
              </button>
            ) : null}
          </div>
        </header>
        <main className="main">
          {toast ? <ToastBanner toast={toast} onClose={() => setToast(null)} /> : null}
          {route.type === "terms" ? (
            <LegalTermsPage navigate={navigate} />
          ) : route.type === "privacy" ? (
            <LegalPrivacyPage navigate={navigate} />
          ) : route.type === "refund" ? (
            <LegalRefundPage navigate={navigate} />
          ) : authStatus !== "authenticated" ? (
            <LoginScreen status={authStatus} apiBase={API_BASE} onLogin={login} onRegister={register} />
          ) : route.type === "admin_billing" && !hasAdminAccess(user?.role || "") ? (
            <AccessDeniedCard
              title="访问受限"
              detail="当前账号不是管理员，无法访问管理页。"
              actionLabel="返回租户工作台"
              onAction={() => navigate("/workspace")}
            />
          ) : route.type === "workspace" ? (
            <TenantWorkspacePage apiFetch={apiFetch} role={user?.role || "user"} pushToast={pushToast} />
          ) : route.type === "billing" ? (
            <BillingPage apiFetch={apiFetch} role={user?.role || "user"} pushToast={pushToast} navigate={navigate} />
          ) : route.type === "admin_billing" ? (
            <AdminBillingPage apiFetch={apiFetch} role={user?.role || "user"} pushToast={pushToast} />
          ) : route.type === "create" ? (
            <CreateCase
              onCreated={(caseId, template) => {
                setFlashCaseId(caseId);
              }}
              pushToast={pushToast}
            />
          ) : (
            <CaseDetail
              caseId={route.caseId}
              onBack={() => navigate("/create")}
              flashCaseId={flashCaseId}
              clearFlash={() => setFlashCaseId(null)}
              pushToast={pushToast}
            />
          )}
        </main>
        {authStatus === "authenticated" ? (
          <PointsPaywallModal
            open={paywallState.open}
            reason={paywallState.reason}
            apiFetch={apiFetch}
            onClose={closePaywall}
            onPaid={handlePaywallPaid}
            pushToast={pushToast}
          />
        ) : null}
        <div className="local-deploy">
          <button
            className="local-deploy-link"
            type="button"
            onClick={() => setLocalDeployOpen((value) => !value)}
            aria-expanded={localDeployOpen}
            aria-label="Local / Private Deployment (Contact Us)"
            title="Local / Private Deployment (Contact Us)"
          >
            <span className="local-deploy-glyph">LP</span>
          </button>
          {localDeployOpen ? (
            <div className="local-deploy-panel" role="dialog" aria-live="polite">
              <div className="local-deploy-title">Local / Private Deployment (Contact Us)</div>
              <div className="local-deploy-body">
                私有/本地部署需要交付团队介入（内网、合规、定制化支持）。
                <br />
                请联系商务或管理员获取对接方式与实施说明。
              </div>
              <button className="text-link" type="button" onClick={() => setLocalDeployOpen(false)}>
                关闭
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </AuthContext.Provider>
  );
}

function ToastBanner({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={`toast-banner ${toast.type === "success" ? "toast-success" : "toast-error"}`}>
      <div>
        <div className="toast-title">{toast.message}</div>
        {toast.code ? <div className="toast-code">{toast.code}</div> : null}
        {toast.detail ? (
          <div className="toast-detail-row">
            <button className="toast-detail" type="button" onClick={() => setExpanded((v) => !v)}>
              {expanded ? "收起详情" : "查看详情"}
            </button>
            <CopyButton
              value={toast.detail}
              label={toast.copyLabel ?? "复制详情"}
              onCopied={() => setExpanded(true)}
            />
          </div>
        ) : null}
        {expanded && toast.detail ? <div className="toast-body">{toast.detail}</div> : null}
      </div>
      <button className="ghost" type="button" onClick={onClose}>
        关闭
      </button>
    </div>
  );
}

function RecommendationSection({
  title,
  items,
  defaultCollapsed = false,
}: {
  title: string;
  items: string[];
  defaultCollapsed?: boolean;
}) {
  const normalizedItems = (items || []).map((item) => String(item || "").trim()).filter(Boolean);
  const [collapsed, setCollapsed] = useState(defaultCollapsed && normalizedItems.length > 0);
  const resetKey = `${title}:${normalizedItems.join("||")}:${defaultCollapsed ? "1" : "0"}`;

  useEffect(() => {
    setCollapsed(defaultCollapsed && normalizedItems.length > 0);
  }, [resetKey, defaultCollapsed, normalizedItems.length]);

  if (!normalizedItems.length) return null;

  return (
    <div className="recommend-section">
      <div className="recommend-section-head">
        <div className="recommend-section-title">{title}</div>
        <button className="text-link" type="button" onClick={() => setCollapsed((value) => !value)}>
          {collapsed ? "展开" : "收起"}
        </button>
      </div>
      {collapsed ? (
        <div className="recommend-section-collapsed">内容已收起，点击“展开”查看。</div>
      ) : (
        <div className="recommend-list-text">
          {normalizedItems.map((line, idx) => (
            <LongText key={`${title}-${idx}`} text={line} lines={2} allowToggle />
          ))}
        </div>
      )}
    </div>
  );
}

function ScoreRadar({ score }: { score?: RecommendScoreBreakdown | null }) {
  if (!score) return null;
  const dimensions = [
    { label: "相关性", value: Number(score.relevance || 0) },
    { label: "热度", value: Number(score.popularity || 0) },
    { label: "成本友好", value: Number(score.cost_bonus || 0) },
    { label: "能力匹配", value: Number(score.capability_match || 0) },
  ];
  const cx = 110;
  const cy = 110;
  const radius = 78;
  const steps = 4;
  const points = dimensions
    .map((item, idx) => {
      const ratio = Math.max(0, Math.min(100, item.value)) / 100;
      const angle = (Math.PI * 2 * idx) / dimensions.length - Math.PI / 2;
      const x = cx + Math.cos(angle) * radius * ratio;
      const y = cy + Math.sin(angle) * radius * ratio;
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <div className="score-radar">
      <svg viewBox="0 0 220 220" role="img" aria-label="方案评分雷达图">
        {Array.from({ length: steps }).map((_, idx) => {
          const ratio = (idx + 1) / steps;
          const ring = dimensions
            .map((_, axis) => {
              const angle = (Math.PI * 2 * axis) / dimensions.length - Math.PI / 2;
              const x = cx + Math.cos(angle) * radius * ratio;
              const y = cy + Math.sin(angle) * radius * ratio;
              return `${x},${y}`;
            })
            .join(" ");
          return <polygon key={`ring-${idx}`} points={ring} className="score-radar-ring" />;
        })}
        {dimensions.map((item, idx) => {
          const angle = (Math.PI * 2 * idx) / dimensions.length - Math.PI / 2;
          const x = cx + Math.cos(angle) * radius;
          const y = cy + Math.sin(angle) * radius;
          return <line key={`axis-${item.label}`} x1={cx} y1={cy} x2={x} y2={y} className="score-radar-axis" />;
        })}
        <polygon points={points} className="score-radar-shape" />
        {dimensions.map((item, idx) => {
          const angle = (Math.PI * 2 * idx) / dimensions.length - Math.PI / 2;
          const x = cx + Math.cos(angle) * (radius + 18);
          const y = cy + Math.sin(angle) * (radius + 18);
          return (
            <text key={`label-${item.label}`} x={x} y={y} className="score-radar-label" textAnchor="middle">
              {item.label}
            </text>
          );
        })}
      </svg>
      <div className="score-radar-total">综合分 {score.final_score}</div>
    </div>
  );
}

function MarkdownSnippet({ markdown, className }: { markdown: string; className?: string }) {
  const html = useMemo(() => {
    const renderer = new marked.Renderer();
    renderer.link = (href, title, text) => {
      const safeHref = String(href || "").trim();
      const safeTitle = String(title || "").trim();
      const titleAttr = safeTitle ? ` title="${safeTitle.replace(/"/g, "&quot;")}"` : "";
      return `<a href="${safeHref}" target="_blank" rel="noreferrer"${titleAttr}>${text}</a>`;
    };
    return marked.parse(String(markdown || ""), { breaks: true, renderer });
  }, [markdown]);

  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}

function CreateCase({
  onCreated,
  pushToast,
}: {
  onCreated: (caseId: string, template?: CaseTemplate | null) => void;
  pushToast: (t: Toast) => void;
}) {
  const { apiFetch } = useAuth();
  const [repoUrl, setRepoUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [status, setStatus] = useState<UnderstandStatusResponse | null>(null);
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);
  const apiIsLocal = API_BASE.includes("127.0.0.1") || API_BASE.includes("localhost");
  const [recommendQuery, setRecommendQuery] = useState("");
  const [recommendFile, setRecommendFile] = useState<File | null>(null);
  const [recommendLoading, setRecommendLoading] = useState(false);
  const [recommendError, setRecommendError] = useState<string | null>(null);
  const [recommendData, setRecommendData] = useState<RecommendResponse | null>(null);
  const [recommendSelected, setRecommendSelected] = useState<string | null>(null);
  const [recommendDeep, setRecommendDeep] = useState(true);
  const [recommendPanelCollapsed, setRecommendPanelCollapsed] = useState(false);
  const [thinkingLogs, setThinkingLogs] = useState<string[]>([]);
  const [thinkingCollapsed, setThinkingCollapsed] = useState(false);
  const [templates, setTemplates] = useState<CaseTemplate[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [recentCases, setRecentCases] = useState<CaseResponse[]>([]);

  useEffect(() => {
    let cancelled = false;
    const loadTemplates = async () => {
      try {
        const response = await apiFetch("/case-templates");
        if (!response.ok) return;
        const data = (await response.json()) as CaseTemplate[];
        if (!cancelled && Array.isArray(data)) {
          setTemplates(data);
        }
      } catch {
        if (!cancelled) {
          setTemplates([]);
        }
      }
    };
    const loadCases = async () => {
      try {
        const response = await apiFetch("/cases?page=1&size=10");
        if (!response.ok) return;
        const data = (await response.json()) as CaseListResponse;
        if (!cancelled && Array.isArray(data.items)) {
          setRecentCases(data.items);
        }
      } catch {
        if (!cancelled) {
          setRecentCases([]);
        }
      }
    };
    loadTemplates();
    loadCases();
    return () => {
      cancelled = true;
    };
  }, [apiFetch]);

  const refreshCase = useCallback(
    async (silent = false): Promise<UnderstandStatusResponse | null> => {
      if (!caseId) return null;
      try {
        const response = await apiFetch(`/cases/${caseId}/status`);
        if (!response.ok) return null;
        const data = (await response.json()) as UnderstandStatusResponse;
        setStatus(data);
        return data;
      } catch (err) {
        if (!silent) {
          const hint = getFetchFailureHint(err, API_BASE);
          setError(hint?.message || "网络错误，请检查 API 连接。");
        }
        return null;
      }
    },
    [caseId, apiFetch]
  );

  const runHealthCheck = useCallback(async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      const response = await apiFetch("/healthz");
      if (!response.ok) {
        throw new Error(`自检失败 (${response.status})`);
      }
      const data = (await response.json()) as HealthReport;
      setHealth(data);
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      setHealthError(hint?.message || "无法连接服务，请检查后端。");
    } finally {
      setHealthLoading(false);
    }
  }, [apiFetch]);

  useEffect(() => {
    if (!caseId) return undefined;
    refreshCase(true);
    const timer = window.setInterval(() => refreshCase(true), 2000);
    return () => window.clearInterval(timer);
  }, [caseId, refreshCase]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!repoUrl.trim()) {
      setError("请输入 GitHub 仓库地址。");
      return;
    }

    const payload: Record<string, unknown> = {
      repo_url: repoUrl.trim(),
      run_mode: "showcase",
    };
    setSubmitting(true);
    try {
      const response = await apiFetch("/cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const text = await response.text();
        setError(text || `请求失败 (${response.status})`);
        return;
      }
      const data = (await response.json()) as CaseResponse;
      await apiFetch(`/cases/${data.case_id}/understand`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      setCaseId(data.case_id);
      setRecentCases((prev) => {
        const merged = [data, ...prev.filter((item) => item.case_id !== data.case_id)];
        return merged.slice(0, 10);
      });
      setStatus({
        case_id: data.case_id,
        repo_url: data.repo_url,
        state: "FETCHING_REPOSITORY",
        message: "正在获取仓库代码…",
      });
      onCreated(data.case_id, null);
      pushToast({
        type: "success",
        message: "开始讲解",
        detail: formatToastDetail({ case_id: data.case_id, repo_url: repoUrl.trim() }),
      });
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      setError(hint?.message || "网络错误，请检查 API 连接。");
    } finally {
      setSubmitting(false);
    }
  };

  const handleRecommend = async (event: React.FormEvent) => {
    event.preventDefault();
    setRecommendError(null);
    if (!recommendQuery.trim() && !recommendFile) {
      setRecommendError("请输入需求描述或上传需求文件。");
      return;
    }
    const formData = new FormData();
    if (recommendQuery.trim()) {
      formData.append("query", recommendQuery.trim());
    }
    formData.append("mode", recommendDeep ? "deep" : "quick");
    formData.append("limit", "10");
    if (recommendFile) {
      formData.append("file", recommendFile);
    }
    setRecommendData(null);
    setRecommendSelected(null);
    setThinkingLogs([]);
    setThinkingCollapsed(false);
    setRecommendLoading(true);
    try {
      const response = await apiFetch("/recommendations/stream", {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        const text = await response.text();
        setRecommendError(text || `请求失败 (${response.status})`);
        return;
      }
      if (!response.body) {
        const fallback = (await response.json()) as RecommendResponse;
        setRecommendData(fallback);
        if (fallback.trace_steps?.length) {
          setThinkingLogs((prev) => {
            const merged = [...prev, ...fallback.trace_steps];
            return Array.from(new Set(merged.map((item) => item.trim()).filter(Boolean)));
          });
        }
        if (fallback.recommendations?.length) {
          setRecommendSelected(fallback.recommendations[0].id);
        }
        setThinkingCollapsed(true);
        pushToast({
          type: "success",
          message: "推荐完成",
          detail: formatToastDetail({ query: recommendQuery.trim(), count: fallback.recommendations?.length || 0 }),
        });
        return;
      }

      const decoder = new TextDecoder();
      const reader = response.body.getReader();
      let buffer = "";
      let streamError: string | null = null;
      let gotResult = false;

      const handleLine = (line: string) => {
        const raw = line.trim();
        if (!raw) return;
        const payload = parseJsonLine<RecommendStreamEvent>(raw);
        if (!payload || typeof payload !== "object") return;
        if (payload.type === "thought") {
          const message = String(payload.message || "").trim();
          if (!message) return;
          setThinkingLogs((prev) => [...prev, message]);
          return;
        }
        if (payload.type === "error") {
          const message = String(payload.message || "推荐服务暂时不可用").trim();
          streamError = message;
          setRecommendError(message);
          setThinkingLogs((prev) => [...prev, `AI 服务异常：${message}`]);
          return;
        }
        if (payload.type === "result" && payload.data) {
          const data = payload.data as RecommendResponse;
          setRecommendData(data);
          if (data.trace_steps?.length) {
            setThinkingLogs((prev) => {
              const merged = [...prev, ...data.trace_steps];
              return Array.from(new Set(merged.map((item) => item.trim()).filter(Boolean)));
            });
          }
          if (data.recommendations?.length) {
            setRecommendSelected(data.recommendations[0].id);
          }
          setThinkingCollapsed(true);
          gotResult = true;
          pushToast({
            type: "success",
            message: "推荐完成",
            detail: formatToastDetail({ query: recommendQuery.trim(), count: data.recommendations?.length || 0 }),
          });
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = splitNdjsonBuffer(buffer);
        buffer = parsed.rest;
        parsed.lines.forEach(handleLine);
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        handleLine(buffer);
      }
      if (!gotResult && !streamError) {
        setRecommendError("推荐流中断：未收到最终结果");
      }
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      setRecommendError(hint?.message || "网络错误，请检查 API 连接。");
    } finally {
      setRecommendLoading(false);
    }
  };

  const stateKey = status?.state || "IDLE";
  const isRunning = ["FETCHING_REPOSITORY", "UNDERSTANDING_CODE", "GENERATING_EXPLANATION"].includes(stateKey);

  const stateTitleMap: Record<string, string> = {
    IDLE: "",
    FETCHING_REPOSITORY: "正在获取仓库代码…",
    UNDERSTANDING_CODE: "正在理解代码逻辑…",
    GENERATING_EXPLANATION: "正在生成讲解内容…",
    DONE: "讲解完成",
    FAILED: "讲解失败",
  };

  const stateDescMap: Record<string, string> = {
    IDLE: "",
    FETCHING_REPOSITORY: "智能助手正在读取仓库结构、README 和关键信息",
    UNDERSTANDING_CODE: "分析核心模块、入口文件和关键实现",
    GENERATING_EXPLANATION: "把代码转化为视频、结构图和重点讲解",
    DONE: "已生成讲解内容",
    FAILED: "出现问题，但仍会展示可用内容",
  };

  const selectedRecommendation = useMemo(() => {
    if (!recommendData?.recommendations?.length) return null;
    return (
      recommendData.recommendations.find((item) => item.id === recommendSelected) ||
      recommendData.recommendations[0]
    );
  }, [recommendData, recommendSelected]);

  const selectedCitations = useMemo(() => {
    const all = recommendData?.citations || [];
    if (!all.length) return [];
    const selectedId = String(selectedRecommendation?.id || "").trim();
    if (!selectedId) return all.slice(0, 8);
    const exact = all.filter((item) => String(item.id || "").trim() === selectedId);
    if (exact.length) return exact.slice(0, 8);
    const fallback = all.filter((item) =>
      String(item.title || "")
        .toLowerCase()
        .includes(String(selectedRecommendation?.full_name || "").toLowerCase())
    );
    return (fallback.length ? fallback : all).slice(0, 8);
  }, [recommendData?.citations, selectedRecommendation?.full_name, selectedRecommendation?.id]);

  useEffect(() => {
    if (!recommendData) return;
    setRecommendPanelCollapsed(false);
  }, [recommendData?.request_id]);

  const executeRecommendationAction = useCallback(
    async (item: RecommendItem) => {
      const action = item.action || null;
      const preferredUrl = String(action?.url || item.official_url || item.html_url || "").trim();
      const repoUrlForDeploy = String(item.repo_url || "").trim();

      if (action?.action_type === "one_click_deploy" && action.deploy_supported && repoUrlForDeploy) {
        try {
          const deployActionResp = await apiFetch(`/products/${encodeURIComponent(item.id)}/deploy`, { method: "POST" });
          if (!deployActionResp.ok) {
            const text = await deployActionResp.text();
            throw new Error(text || `resolve deploy action failed (${deployActionResp.status})`);
          }
          const deployAction = (await deployActionResp.json()) as RecommendAction;
          if (String(deployAction.action_type || "").trim() !== "one_click_deploy") {
            const fallbackUrl = String(deployAction.url || preferredUrl || "").trim();
            if (fallbackUrl) {
              window.open(fallbackUrl, "_blank", "noopener,noreferrer");
              return;
            }
            throw new Error("产品未返回可执行部署动作");
          }

          const response = await apiFetch("/cases", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo_url: repoUrlForDeploy, run_mode: "deploy" }),
          });
          if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `create deploy case failed (${response.status})`);
          }
          const data = (await response.json()) as CaseResponse;
          onCreated(data.case_id, null);
          pushToast({
            type: "success",
            message: "已触发一键部署",
            detail: formatToastDetail({ case_id: data.case_id, repo_url: repoUrlForDeploy }),
          });
          return;
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          pushToast({ type: "error", message: "部署动作执行失败", detail: message });
          return;
        }
      }

      if (preferredUrl) {
        window.open(preferredUrl, "_blank", "noopener,noreferrer");
        return;
      }
      pushToast({ type: "error", message: "缺少可跳转链接", detail: `product_id=${item.id}` });
    },
    [apiFetch, onCreated, pushToast]
  );

  return (
    <div className="create-grid" data-testid="page-create">
      <section className="card card-hero" style={{ animationDelay: "0.05s" }}>
        <div className="section-title" data-testid="page-title-library">一键看懂仓库</div>
        <p className="section-sub">粘贴仓库链接，30–60 秒后就能看懂核心逻辑。</p>
        <form className="form" onSubmit={handleSubmit} data-testid="create-form">
          <label className="field">
            <span>模板（可选）</span>
            <select
              data-testid="template-select"
              value={selectedTemplate}
              onChange={(event) => {
                const next = event.target.value;
                setSelectedTemplate(next);
                const matched = templates.find((item) => item.name === next);
                if (matched?.repo_url || matched?.git_url) {
                  setRepoUrl((matched.repo_url || matched.git_url || "").trim());
                }
              }}
              disabled={isRunning || submitting}
            >
              <option value="">手动输入</option>
              {templates.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>GitHub 仓库地址</span>
            <input
              type="text"
              placeholder="粘贴 GitHub 仓库链接，例如：https://github.com/owner/repo"
              value={repoUrl}
              onChange={(event) => setRepoUrl(event.target.value)}
              disabled={isRunning || submitting}
            />
          </label>
          <div className="muted">无需运行仓库，智能助手会直接讲清代码在做什么。</div>
          {error ? <div className="error-banner">{error}</div> : null}
          {error && apiIsLocal ? (
            <div className="muted">如你启用了系统代理，请将 127.0.0.1/localhost 设为直连。</div>
          ) : null}
          <button className="primary" type="submit" disabled={submitting || isRunning}>
            {submitting || isRunning ? "正在理解..." : "一键看懂仓库"}
          </button>
          <button className="ghost" type="button" onClick={runHealthCheck} disabled={healthLoading}>
            {healthLoading ? "自检中…" : "运行环境自检"}
          </button>
        </form>
        <div className="case-list" data-testid="page-dashboard">
          <div className="section-title" data-testid="page-title-dashboard">最近案例</div>
          {recentCases.length ? (
            <div className="recommend-list">
              {recentCases.map((item) => (
                <div key={item.case_id} className="recommend-item" data-testid={`case-card-${item.case_id}`}>
                  <div className="recommend-item-head">
                    <div className="recommend-item-title">{item.case_id}</div>
                    <div className="recommend-score">{item.status}</div>
                  </div>
                  <LongText text={item.repo_url || "无仓库地址"} lines={2} allowToggle className="muted" />
                </div>
              ))}
            </div>
          ) : (
            <div className="muted">暂无案例。</div>
          )}
        </div>
        {healthError ? <div className="error-banner">{healthError}</div> : null}
        {health ? (
          <div className="health-card">
            <div className="health-title">运行环境检查</div>
            <div className="health-meta">API 地址：{API_BASE}</div>
            <div className="health-grid">
              <div className={`health-item ${health.redis === "ok" ? "ok" : "bad"}`}>
                <span>Redis 缓存</span>
                <span>{health.redis === "ok" ? "可用" : "未就绪"}</span>
              </div>
              <div className={`health-item ${health.openclaw === "ok" ? "ok" : "bad"}`}>
                <span>OpenClaw 抓取服务</span>
                <span>{health.openclaw === "ok" ? "可用" : "未就绪"}</span>
              </div>
              <div className={`health-item ${health.docker === "ok" ? "ok" : "bad"}`}>
                <span>Docker（可选）</span>
                <span>{health.docker === "ok" ? "可用" : "未就绪"}</span>
              </div>
            </div>
            {health.openclaw !== "ok" ? (
              <div className="health-hint">缺少 OpenClaw：运行 `scripts/dev_services.sh up` 启动。</div>
            ) : null}
            {health.redis !== "ok" ? (
              <div className="health-hint">缺少 Redis：运行 `scripts/dev_services.sh up` 启动。</div>
            ) : null}
          </div>
        ) : null}
        {stateKey !== "IDLE" ? (
          <div className="state-card">
            {isRunning ? <div className="spinner" aria-hidden="true" /> : null}
            <div className="state-title">{stateTitleMap[stateKey]}</div>
            <div className="state-desc">{stateDescMap[stateKey]}</div>
          </div>
        ) : null}
      </section>

      <section className="card card-glow" style={{ animationDelay: "0.1s" }}>
        <div className="section-title">技术选型决策引擎</div>
        <p className="section-sub">
          上传需求文档或输入一句话需求，统一比较开源与商业方案，并给出可解释的评分与动作建议。
        </p>
        <form className="form" onSubmit={handleRecommend}>
          <label className="field">
            <span>需求摘要</span>
            <textarea
              placeholder="例如：需要一套支持多租户、RBAC、审计日志的后台管理系统"
              value={recommendQuery}
              onChange={(event) => setRecommendQuery(event.target.value)}
              disabled={recommendLoading}
              rows={4}
            />
          </label>
          <label className="field">
            <span>需求文件（可选，支持 .pdf .docx .md .txt）</span>
            <input
              type="file"
              accept=".pdf,.docx,.md,.txt"
              onChange={(event) => {
                const file = event.target.files?.[0] || null;
                setRecommendFile(file);
              }}
              disabled={recommendLoading}
            />
            {recommendFile ? <div className="muted">已选择：{recommendFile.name}</div> : null}
          </label>
          <label className="field-inline">
            <input
              type="checkbox"
              checked={recommendDeep}
              onChange={(event) => setRecommendDeep(event.target.checked)}
              disabled={recommendLoading}
            />
            <span>开启深度评估（会稍慢）</span>
          </label>
          {recommendError ? <div className="error-banner">{recommendError}</div> : null}
          <button className="primary" type="submit" disabled={recommendLoading}>
            {recommendLoading ? "正在匹配..." : "智能推荐方案"}
          </button>
        </form>
        {recommendLoading || thinkingLogs.length ? (
          <div className={`thinking-console ${thinkingCollapsed ? "thinking-console-collapsed" : ""}`}>
            <div className="thinking-console-head">
              <div className="section-title">动态构建中</div>
              <div className="muted">{recommendLoading ? "实时分析日志" : "分析完成"}</div>
            </div>
            <div className="thinking-console-body">
              {thinkingLogs.length ? (
                thinkingLogs.map((line, idx) => (
                  <div className="thinking-line" key={`thinking-${idx}-${line}`}>
                    {line}
                  </div>
                ))
              ) : (
                <>
                  <div className="thinking-skeleton" />
                  <div className="thinking-skeleton" />
                  <div className="thinking-skeleton" />
                </>
              )}
              {recommendLoading ? <div className="thinking-cursor">▍</div> : null}
            </div>
          </div>
        ) : null}
        {recommendData ? (
          <div className="recommendation-panel">
            <div className="recommend-panel-head">
              <div className="section-title">匹配结果（{recommendData.recommendations?.length || 0}）</div>
              <button
                className="ghost"
                type="button"
                onClick={() => setRecommendPanelCollapsed((value) => !value)}
              >
                {recommendPanelCollapsed ? "展开结果" : "收起结果"}
              </button>
            </div>
            {recommendPanelCollapsed ? (
              <div className="recommend-collapsed-note">推荐结果已收起，点击“展开结果”继续查看。</div>
            ) : (
              <>
                {recommendData.profile?.summary ? (
                  <div className="info-card">
                    <div className="section-title">需求画像</div>
                    <LongText text={recommendData.profile.summary} lines={3} allowToggle className="muted" />
                    {recommendData.profile.keywords?.length ? (
                      <div className="case-card-pills">
                        {recommendData.profile.keywords.slice(0, 8).map((item) => (
                          <span className="pill" key={item}>
                            {item}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {recommendData.deep_summary || recommendData.insight_points?.length ? (
                  <div className="info-card">
                    <div className="section-title">深度搜索结论</div>
                    {recommendData.deep_summary ? (
                      <LongText text={recommendData.deep_summary} lines={4} allowToggle className="muted" />
                    ) : null}
                    {recommendData.insight_points?.length ? (
                      <div className="recommend-list-text">
                        {recommendData.insight_points.slice(0, 6).map((item, idx) => (
                          <LongText key={`insight-${idx}`} text={item} lines={2} allowToggle />
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {recommendData.citations?.length ? (
                  <div className="recommend-citation-strip">
                    <div className="section-title">关键引文</div>
                    <div className="citation-chip-list">
                      {recommendData.citations.slice(0, 8).map((item, idx) => (
                        <a
                          key={`${item.id}-${idx}`}
                          className="citation-chip"
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <span className="mono">{item.source}</span>
                          <span>{item.title}</span>
                        </a>
                      ))}
                    </div>
                  </div>
                ) : null}
                {recommendData.warnings?.length ? (
                  <div className="warning-banner">
                    {recommendData.warnings.map((item, idx) => (
                      <LongText key={`${item}-${idx}`} text={item} lines={2} allowToggle />
                    ))}
                  </div>
                ) : null}
                <div className="recommend-grid">
                  <div className="recommend-list">
                    {(recommendData.recommendations || []).map((item) => (
                      <button
                        type="button"
                        key={item.id}
                        className={`recommend-item ${recommendSelected === item.id ? "active" : ""}`}
                        onClick={() => setRecommendSelected(item.id)}
                      >
                        <div className="recommend-item-head">
                          <div>
                            <div className="recommend-item-title">{item.full_name}</div>
                            <div className="recommend-meta">
                              <span className={`pill ${productTypePillClass(item.product_type)}`}>
                                {productTypeLabel(item.product_type)}
                              </span>
                            </div>
                          </div>
                          <div className="recommend-score">{item.match_score ?? 0}</div>
                        </div>
                        <LongText
                          text={item.description || "暂无仓库描述"}
                          lines={2}
                          allowToggle
                          className="muted recommend-description"
                        />
                        <div className="recommend-meta">
                          <span>动作 {(item.action?.label || "查看方案").trim()}</span>
                          <span>热度 {item.score_breakdown?.popularity ?? "-"}</span>
                          <span>能力匹配 {item.score_breakdown?.capability_match ?? "-"}</span>
                        </div>
                        <div className="case-card-pills">
                          {(
                            item.capabilities?.map((capability) => capability.name) ||
                            (item.match_tags && item.match_tags.length ? item.match_tags : item.topics || [])
                          )
                            .slice(0, 4)
                            .map((tag) => (
                              <span className="pill" key={tag}>
                                {tag}
                              </span>
                            ))}
                        </div>
                      </button>
                    ))}
                  </div>
                  <div className="recommend-detail">
                    {selectedRecommendation ? (
                      <>
                        <div className="recommend-detail-head">
                          <div>
                            <div className="recommend-detail-title">{selectedRecommendation.full_name}</div>
                            <div className="case-card-pills">
                              <span className={`pill ${productTypePillClass(selectedRecommendation.product_type)}`}>
                                {productTypeLabel(selectedRecommendation.product_type)}
                              </span>
                            </div>
                            <LongText
                              text={selectedRecommendation.description || "暂无仓库描述"}
                              lines={3}
                              allowToggle
                              className="muted recommend-description-detail"
                            />
                          </div>
                          <div className="row-actions recommend-detail-actions">
                            <button
                              className="ghost"
                              type="button"
                              onClick={() => executeRecommendationAction(selectedRecommendation)}
                              disabled={
                                !(selectedRecommendation.action?.deploy_supported && selectedRecommendation.repo_url) &&
                                !String(
                                  selectedRecommendation.action?.url ||
                                    selectedRecommendation.official_url ||
                                    selectedRecommendation.html_url ||
                                    ""
                                ).trim()
                              }
                            >
                              {(selectedRecommendation.action?.label || "查看方案").trim()}
                            </button>
                            {String(selectedRecommendation.repo_url || selectedRecommendation.html_url || "").trim() ? (
                              <a
                                className="recommend-source-link"
                                href={String(selectedRecommendation.repo_url || selectedRecommendation.html_url || "").trim()}
                                target="_blank"
                                rel="noreferrer"
                                title="查看原始开源仓库"
                              >
                                查看源码 / View Source
                              </a>
                            ) : null}
                          </div>
                        </div>
                        {selectedRecommendation.health ? (
                          <div className="recommend-health-grid">
                            <div className="stat-card">
                              <div className="stat-title">综合评分</div>
                              <div className="stat-value">
                                {selectedRecommendation.health.overall_score} / {selectedRecommendation.health.grade}
                              </div>
                            </div>
                            <div className="stat-card">
                              <div className="stat-title">{selectedRecommendation.health.activity.label}</div>
                              <div className="stat-value">{selectedRecommendation.health.activity.score}</div>
                            </div>
                            <div className="stat-card">
                              <div className="stat-title">{selectedRecommendation.health.community.label}</div>
                              <div className="stat-value">{selectedRecommendation.health.community.score}</div>
                            </div>
                            <div className="stat-card">
                              <div className="stat-title">{selectedRecommendation.health.maintenance.label}</div>
                              <div className="stat-value">{selectedRecommendation.health.maintenance.score}</div>
                            </div>
                          </div>
                        ) : null}
                        {selectedRecommendation.score_breakdown ? (
                          <div className="recommend-score-layout">
                            <ScoreRadar score={selectedRecommendation.score_breakdown} />
                            <div className="recommend-score-formula">
                              <div className="section-title">混合评分公式</div>
                              <div className="muted mono">
                                Score = Relevance * 0.4 + Popularity * 0.2 + CostBonus * 0.15 + CapabilityMatch * 0.25
                              </div>
                              <div className="recommend-meta">
                                <span>Relevance {selectedRecommendation.score_breakdown.relevance}</span>
                                <span>Popularity {selectedRecommendation.score_breakdown.popularity}</span>
                                <span>CostBonus {selectedRecommendation.score_breakdown.cost_bonus}</span>
                                <span>Capability {selectedRecommendation.score_breakdown.capability_match}</span>
                              </div>
                            </div>
                          </div>
                        ) : null}
                        {selectedRecommendation.health?.signals?.length ? (
                          <div className="case-card-pills">
                            {selectedRecommendation.health.signals.map((signal) => (
                              <span className="pill" key={signal}>
                                {signal}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        <RecommendationSection
                          key={`match-${selectedRecommendation.id}`}
                          title="适用性分析"
                          items={selectedRecommendation.match_reasons || []}
                          defaultCollapsed={(selectedRecommendation.match_reasons || []).length > 3}
                        />
                        <RecommendationSection
                          key={`risk-${selectedRecommendation.id}`}
                          title="风险提示"
                          items={selectedRecommendation.risk_notes || []}
                          defaultCollapsed={(selectedRecommendation.risk_notes || []).length > 2}
                        />
                        <RecommendationSection
                          key={`maintenance-${selectedRecommendation.id}`}
                          title="维护关注点"
                          items={selectedRecommendation.health?.warnings || []}
                          defaultCollapsed={(selectedRecommendation.health?.warnings || []).length > 2}
                        />
                        {selectedCitations.length ? (
                          <div className="recommend-section">
                            <div className="recommend-section-head">
                              <div className="recommend-section-title">引用来源</div>
                            </div>
                            <div className="citation-list">
                              {selectedCitations.map((item, idx) => (
                                <a
                                  key={`${item.id}-${idx}`}
                                  className="citation-item"
                                  href={item.url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  <div className="citation-item-head">
                                    <span className="mono">{item.source}</span>
                                    <span className="citation-score">{item.score ?? "-"}</span>
                                  </div>
                                  <div className="citation-title">{item.title}</div>
                                  {item.snippet ? (
                                    <MarkdownSnippet markdown={item.snippet} className="citation-snippet markdown citation-markdown" />
                                  ) : null}
                                </a>
                              ))}
                            </div>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <div className="muted">
                        {(recommendData.recommendations || []).length
                          ? "选择左侧仓库查看详情卡片。"
                          : "未检索到高匹配仓库，请补充更具体约束（技术栈、数据来源、目标输出）。"}
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        ) : null}
      </section>

      {caseId ? (
        <VisualsPanel
          caseId={caseId}
          visualStatus={status?.visual_status}
          visualReady={status?.visual_ready}
          visualErrorCode={status?.visual_error_code}
          pushToast={pushToast}
          refreshCase={refreshCase}
        />
      ) : null}
    </div>
  );
}
function CaseDetail({
  caseId,
  onBack,
  flashCaseId,
  clearFlash,
  pushToast,
}: {
  caseId: string;
  onBack: () => void;
  flashCaseId: string | null;
  clearFlash: () => void;
  pushToast: (t: Toast) => void;
}) {
  const { apiFetch } = useAuth();
  const [caseData, setCaseData] = useState<UnderstandStatusResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const fetchLock = useRef(false);

  const fetchCase = useCallback(
    async (silent = false): Promise<UnderstandStatusResponse | null> => {
      if (fetchLock.current) return null;
      fetchLock.current = true;
      if (!silent) {
        setLoading(true);
      }
      setLoadError(null);
      try {
        let response = await apiFetch(`/cases/${caseId}/status`);
        let data: UnderstandStatusResponse | null = null;
        if (response.ok) {
          data = (await response.json()) as UnderstandStatusResponse;
        } else {
          const fallback = await apiFetch(`/cases/${caseId}`);
          if (fallback.ok) {
            const legacy = (await fallback.json()) as CaseResponse;
            data = {
              case_id: legacy.case_id,
              repo_url: legacy.repo_url || null,
              state: legacy.status || "IDLE",
              message: legacy.error_message || "",
              visual_status: legacy.visual_status || null,
              visual_ready: legacy.visual_ready || false,
              visual_error_code: legacy.visual_error_code || null,
              visual_error_message: legacy.visual_error_message || null,
              updated_at: legacy.updated_at || null,
              runtime: legacy.runtime || null,
            } as UnderstandStatusResponse;
          }
        }
        if (!data) {
          let message = `请求失败 (${response.status})`;
          try {
            const body = await response.json();
            if (body && body.detail) {
              message = body.detail;
            }
          } catch (err) {
            const text = await response.text();
            if (text) message = text;
          }
          setLoadError(message);
          pushToast({
            type: "error",
            message: "加载失败",
            detail: formatToastDetail({ case_id: caseId, error: message }),
          });
          return null;
        }
        setCaseData(data);
        return data;
      } catch (err) {
        const hint = getFetchFailureHint(err, API_BASE);
        if (hint) {
          setLoadError(hint.message);
          pushToast({
            type: "error",
            message: hint.message,
            detail: formatToastDetail({ case_id: caseId }, hint.detail),
          });
        } else {
          setLoadError("网络错误，请检查 API 连接。");
        }
        return null;
      } finally {
        fetchLock.current = false;
        setLoading(false);
      }
    },
    [caseId, pushToast, apiFetch]
  );

  useEffect(() => {
    fetchCase();
  }, [fetchCase]);

  useEffect(() => {
    const handler = () => fetchCase();
    window.addEventListener("antihub:case-refresh", handler);
    return () => window.removeEventListener("antihub:case-refresh", handler);
  }, [fetchCase]);

  const repoUrl = caseData?.repo_url || "";
  const [deploying, setDeploying] = useState(false);
  const [deployPolling, setDeployPolling] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);
  const accessUrl = caseData?.runtime?.access_url || null;

  const triggerDeploy = useCallback(async () => {
    setDeploying(true);
    setDeployError(null);
    try {
      const response = await apiFetch(`/cases/${caseId}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `部署失败 (${response.status})`);
      }
      const data = (await response.json()) as CaseActionResponse;
      pushToast({
        type: "success",
        message: "已触发 One-Click Deploy",
        detail: formatToastDetail({ case_id: caseId, action: data.action }),
      });
      setDeployPolling(true);
      await fetchCase(true);
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      const message = hint?.message || "部署触发失败，请稍后重试。";
      setDeployError(message);
      pushToast({
        type: "error",
        message,
        detail: formatToastDetail({ case_id: caseId }, hint?.detail ?? String(err)),
      });
    } finally {
      setDeploying(false);
    }
  }, [caseId, fetchCase, pushToast, apiFetch]);

  useEffect(() => {
    if (!deployPolling) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    let attempts = 0;
    const poll = async () => {
      if (cancelled) return;
      const data = await fetchCase(true);
      attempts += 1;
      if (data?.runtime?.access_url) {
        setDeployPolling(false);
        pushToast({
          type: "success",
          message: "部署完成，已生成访问地址",
          detail: formatToastDetail({ case_id: caseId, access_url: data.runtime.access_url }),
        });
        return;
      }
      if (attempts >= 30) {
        setDeployPolling(false);
        return;
      }
      timer = window.setTimeout(poll, 3000);
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [caseId, deployPolling, fetchCase, pushToast]);

  return (
    <div className="detail" data-testid="page-case">
      <div className="page-title" data-testid="page-title-detail">
        <button type="button" className="ghost" onClick={onBack}>
          返回
        </button>
        <div>
          <div className="title">智能讲代码</div>
          <div className="muted">一键看懂仓库</div>
        </div>
      </div>
      {flashCaseId ? <div className="success-banner">已开始讲解，案例 ID：{flashCaseId}</div> : null}
      {loadError ? <div className="error-banner">{loadError}</div> : null}

      <section className="case-header card card-glow" data-testid="case-header">
        <div className="case-header-main">
          <div className="case-title-row">
            <LongText text={caseId} lines={1} allowToggle={false} className="case-id" mono />
            <CopyButton
              value={caseId}
              label="复制案例 ID"
              onCopied={() =>
                pushToast({
                  type: "success",
                  message: "已复制案例 ID",
                  detail: formatToastDetail({ case_id: caseId }),
                })
              }
            />
          </div>
          <div className="case-meta-grid">
            <CopyField
              label="仓库地址"
              value={repoUrl || null}
              mono
              lines={2}
              allowToggle
              onCopied={() =>
                repoUrl &&
                pushToast({
                  type: "success",
                  message: "已复制仓库地址",
                  detail: formatToastDetail({ repo_url: repoUrl }),
                })
              }
            />
          </div>
        </div>
        <div className="case-header-actions">
          <button
            className="primary"
            type="button"
            onClick={triggerDeploy}
            disabled={deploying || deployPolling}
            data-testid="case-one-click-deploy"
          >
            {deploying || deployPolling ? "部署中…" : "One-Click Deploy"}
          </button>
          <div className="deploy-meta">
            {accessUrl ? (
              <a className="deploy-link" href={accessUrl} target="_blank" rel="noreferrer">
                访问地址
              </a>
            ) : deployPolling ? (
              <span className="muted">正在部署，完成后返回访问地址。</span>
            ) : (
              <span className="muted">点击部署后自动生成访问地址。</span>
            )}
            {deployError ? <span className="deploy-error">{deployError}</span> : null}
          </div>
        </div>
      </section>

      <VisualsPanel
        caseId={caseId}
        visualStatus={caseData?.visual_status}
        visualReady={caseData?.visual_ready}
        visualErrorCode={caseData?.visual_error_code}
        pushToast={pushToast}
        refreshCase={fetchCase}
      />
    </div>
  );
}
function VisualsPanel({
  caseId,
  visualStatus,
  visualReady,
  visualErrorCode,
  pushToast,
  refreshCase,
}: {
  caseId: string;
  visualStatus?: string | null;
  visualReady?: boolean;
  visualErrorCode?: string | null;
  pushToast: (t: Toast) => void;
  refreshCase: (silent?: boolean) => Promise<UnderstandStatusResponse | null>;
}) {
  const { apiFetch, token } = useAuth();
  const [visuals, setVisuals] = useState<UnderstandResultResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [repoGraph, setRepoGraph] = useState<RepoGraph | null>(null);
  const [knowledgeGraph, setKnowledgeGraph] = useState<KnowledgeGraph | null>(null);
  const [activeKnowledgeGraph, setActiveKnowledgeGraph] = useState<KnowledgeGraph | null>(null);
  const [knowledgeAnalysis, setKnowledgeAnalysis] = useState<KnowledgeGraphAnalysis | null>(null);
  const [spotlights, setSpotlights] = useState<Spotlights | null>(null);
  const [storyboard, setStoryboard] = useState<Storyboard | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [productStory, setProductStory] = useState<ProductStory | null>(null);
  const [productStoryLoading, setProductStoryLoading] = useState(false);
  const [productStoryError, setProductStoryError] = useState<string | null>(null);
  const [repoIndex, setRepoIndex] = useState<RepoIndex | null>(null);
  const [ingestMeta, setIngestMeta] = useState<IngestMeta | null>(null);
  const [activeTab, setActiveTab] = useState<"logs" | "manual">("logs");
  const [manualData, setManualData] = useState<ManualResponse | null>(null);
  const [manualLoading, setManualLoading] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);

  const fetchVisuals = useCallback(async () => {
    setLoading(true);
    try {
      const response = await apiFetch(`/cases/${caseId}/result`);
      if (!response.ok) {
        throw new Error(`获取讲解内容失败 (${response.status})`);
      }
      const data = (await response.json()) as UnderstandResultResponse;
      setVisuals(data);
      return data;
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      pushToast({
        type: "error",
        message: hint?.message ?? "获取讲解内容失败",
        detail: formatToastDetail({ case_id: caseId }, hint?.detail ?? String(err)),
      });
      return null;
    } finally {
      setLoading(false);
    }
  }, [caseId, pushToast, apiFetch]);

  const findAssetFile = useCallback(
    (kind: string) => {
      if (!visuals) return null;
      const asset = visuals.assets.find((item) => item.kind === kind);
      if (asset?.files?.length) return asset.files[0];
      const byName = visuals.assets
        .flatMap((item) => item.files || [])
        .find((file) => file.name === `${kind}.json`);
      return byName || null;
    },
    [visuals]
  );

  const fetchJsonFile = useCallback(async (file: VisualFile | null) => {
    if (!file) return null;
    const response = await apiFetch(file.url);
    if (!response.ok) return null;
    return response.json();
  }, [apiFetch]);

  const fetchLogs = useCallback(async () => {
    setLogsLoading(true);
    setLogsError(null);
    try {
      const response = await apiFetch(`/cases/${caseId}/logs?limit=200`);
      if (!response.ok) {
        throw new Error(`日志获取失败 (${response.status})`);
      }
      const data = (await response.json()) as LogEntry[];
      setLogEntries(Array.isArray(data) ? data : []);
    } catch (err) {
      const hint = getFetchFailureHint(err, API_BASE);
      setLogsError(hint?.message || "日志获取失败，请稍后重试。");
    } finally {
      setLogsLoading(false);
    }
  }, [caseId, apiFetch]);

  const assetUrl = useCallback(
    (path: string) => {
      const url = joinUrl(API_BASE, path);
      if (!token) return url;
      const sep = url.includes("?") ? "&" : "?";
      return `${url}${sep}token=${encodeURIComponent(token)}`;
    },
    [token]
  );

  useEffect(() => {
    if (visualReady) {
      fetchVisuals();
    } else if (visualStatus && ["PENDING", "RUNNING"].includes(visualStatus.toUpperCase())) {
      setPolling(true);
    }
  }, [visualReady, visualStatus, fetchVisuals]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!visuals) {
        setRepoGraph(null);
        setKnowledgeGraph(null);
        setActiveKnowledgeGraph(null);
        setKnowledgeAnalysis(null);
        setSpotlights(null);
        setStoryboard(null);
        setProductStory(null);
        setProductStoryError(null);
        setProductStoryLoading(false);
        setRepoIndex(null);
        setIngestMeta(null);
        return;
      }
      const [graphFile, knowledgeFile, spotlightFile, storyboardFile] = [
        findAssetFile("repo_graph"),
        findAssetFile("knowledge_graph"),
        findAssetFile("spotlights"),
        findAssetFile("storyboard"),
      ];
      const [indexFile, ingestFile] = [findAssetFile("repo_index"), findAssetFile("ingest_meta")];
      const storyFile = findAssetFile("product_story");
      setProductStoryLoading(true);
      const [graphData, knowledgeData, spotlightData, storyboardData, storyData, indexData, ingestData] = await Promise.all([
        fetchJsonFile(graphFile),
        fetchJsonFile(knowledgeFile),
        fetchJsonFile(spotlightFile),
        fetchJsonFile(storyboardFile),
        fetchJsonFile(storyFile),
        fetchJsonFile(indexFile),
        fetchJsonFile(ingestFile),
      ]);
      if (cancelled) return;
      const parsedKnowledge = parseKnowledgeGraphAsset(knowledgeData);
      setRepoGraph(graphData);
      setKnowledgeGraph(parsedKnowledge.graph);
      setActiveKnowledgeGraph(parsedKnowledge.graph);
      setKnowledgeAnalysis(parsedKnowledge.analysis);
      setSpotlights(spotlightData);
      setStoryboard(storyboardData);
      setProductStory(storyData);
      setRepoIndex(indexData);
      setIngestMeta(ingestData);
      if (storyFile && !storyData) {
        setProductStoryError("产品叙事加载失败。");
      } else {
        setProductStoryError(null);
      }
      setProductStoryLoading(false);
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [visuals, findAssetFile, fetchJsonFile]);

  useEffect(() => {
    if (!polling) return undefined;
    let timer: number | undefined;
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      const data = await refreshCase(true);
      if (data?.visual_ready) {
        await fetchVisuals();
        setPolling(false);
        return;
      }
      const status = (data?.visual_status || "").toUpperCase();
      if (status === "FAILED" || status === "SUCCESS" || status === "PARTIAL") {
        await fetchVisuals();
        setPolling(false);
        return;
      }
      timer = window.setTimeout(poll, 2000);
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [polling, fetchVisuals, refreshCase]);

  useEffect(() => {
    if (!logsOpen) return undefined;
    fetchLogs();
    const status = (visualStatus || "").toUpperCase();
    if (status !== "PENDING" && status !== "RUNNING") return undefined;
    const timer = window.setInterval(fetchLogs, 2000);
    return () => window.clearInterval(timer);
  }, [logsOpen, fetchLogs, visualStatus]);

  useEffect(() => {
    let cancelled = false;
    const loadManual = async () => {
      setManualLoading(true);
      setManualError(null);
      try {
        const response = await apiFetch(`/cases/${caseId}/manual`);
        if (!response.ok) {
          throw new Error(`manual ${response.status}`);
        }
        const data = (await response.json()) as ManualResponse;
        if (!cancelled) {
          setManualData(data);
        }
      } catch (err) {
        if (!cancelled) {
          const hint = getFetchFailureHint(err, API_BASE);
          setManualError(hint?.message || "说明书加载失败。");
          setManualData(null);
        }
      } finally {
        if (!cancelled) {
          setManualLoading(false);
        }
      }
    };
    loadManual();
    return () => {
      cancelled = true;
    };
  }, [caseId, apiFetch]);

  const effectiveStatus = visualReady ? visuals?.status || visualStatus || "SUCCESS" : visualStatus || "PENDING";
  const normalizedStatus = (effectiveStatus || "").toUpperCase();
  const displayErrorHint = resolveErrorHint(visualErrorCode, {});

  const videoFile = useMemo(() => {
    if (!visuals) return null;
    const videoAsset = visuals.assets.find((asset) => asset.kind === "video");
    const byAsset = videoAsset?.files?.find(
      (file) => file.name.toLowerCase().endsWith(".mp4") || file.name.toLowerCase().endsWith(".webm")
    );
    if (byAsset) return byAsset;
    const fallback = visuals.assets
      .flatMap((asset) => asset.files || [])
      .find((file) => file.name.toLowerCase().endsWith(".mp4") || file.name.toLowerCase().endsWith(".webm"));
    return fallback || null;
  }, [visuals]);

  const evidenceCatalog = storyboard?.evidence_catalog || [];
  const productStoryEvidenceCatalog = Array.isArray(productStory?.meta?.evidence_catalog)
    ? productStory?.meta?.evidence_catalog
    : [];
  const mergedEvidenceCatalog = useMemo(
    () => mergeEvidenceCatalogs(evidenceCatalog, productStoryEvidenceCatalog),
    [evidenceCatalog, productStoryEvidenceCatalog]
  );
  const evidenceById = useMemo(() => {
    const map = new Map<string, Evidence>();
    (evidenceCatalog || []).forEach((item) => {
      if (isEvidenceValid(item)) map.set(item.id, item);
    });
    return map;
  }, [evidenceCatalog]);

  const findSceneEvidence = useCallback(
    (sceneId: string) => {
      const scenes = Array.isArray(storyboard?.scenes) ? storyboard?.scenes : [];
      const scene = scenes.find((item) => item?.id === sceneId);
      const shots = Array.isArray(scene?.shots) ? scene?.shots : [];
      for (const shot of shots) {
        const evidenceId = shot?.evidence_id;
        if (evidenceId && evidenceById.has(evidenceId)) {
          return evidenceById.get(evidenceId) || null;
        }
      }
      return null;
    },
    [storyboard, evidenceById]
  );

  const graphEvidence = findSceneEvidence("graph");
  const videoEvidenceList = useMemo(() => {
    const scenes = Array.isArray(storyboard?.scenes) ? storyboard?.scenes : [];
    const ids = new Set<string>();
    scenes.forEach((scene) => {
      const shots = Array.isArray(scene?.shots) ? scene?.shots : [];
      shots.forEach((shot) => {
        const evidenceId = shot?.evidence_id;
        if (evidenceId && evidenceById.has(evidenceId)) ids.add(evidenceId);
      });
    });
    return Array.from(ids)
      .map((id) => evidenceById.get(id))
      .filter((item): item is Evidence => Boolean(item));
  }, [storyboard, evidenceById]);

  const spotlightItems = (spotlights?.items || []).filter(
    (item) => item && item.line_range && isEvidenceValid(item.evidence)
  );

  const posterFile = useMemo(() => findAssetFile("architecture_poster"), [findAssetFile]);
  const pipelineFile = useMemo(() => findAssetFile("pipeline_sequence"), [findAssetFile]);
  const posterAsset = useMemo(
    () => visuals?.assets.find((asset) => asset.kind === "architecture_poster") || null,
    [visuals]
  );
  const posterFallback = useMemo(() => {
    const meta = (posterAsset?.meta as Record<string, unknown> | undefined) || {};
    return meta.fallback === "reference";
  }, [posterAsset]);
  const posterNotice = useMemo(() => {
    if (!posterFallback) return null;
    type PosterMeta = {
      fallback?: string;
      validation?: { minimax?: { error?: unknown } };
    } & Record<string, unknown>;

    const meta = (posterAsset?.meta as PosterMeta | undefined) || {};
    const error = meta.validation?.minimax?.error;
    if (typeof error === "string") {
      if (error.includes("VISUAL_IMAGE_MODEL")) return "未配置 VISUAL_IMAGE_MODEL，已使用参考图。";
      if (error.includes("VISUAL_API_KEY")) return "未配置 VISUAL_API_KEY，已使用参考图。";
    }
    return "图像模型不可用，已使用参考图。";
  }, [posterAsset, posterFallback]);
  const posterEvidence = useMemo(
    () => pickEvidence(mergedEvidenceCatalog, ["structure", "dependency", "readme"]),
    [mergedEvidenceCatalog]
  );
  const pipelineEvidence = useMemo(
    () => pickEvidence(mergedEvidenceCatalog, ["structure", "dependency"]),
    [mergedEvidenceCatalog]
  );

  const highlightRangesFor = (item: Spotlights["items"][number]) => {
    if (item.line_range?.start && item.line_range?.end) {
      return [{ start_line: item.line_range.start, end_line: item.line_range.end }];
    }
    if (Array.isArray(item.highlights) && item.highlights.length) {
      return item.highlights.filter((range) => range && range.start_line && range.end_line);
    }
    if (item.start_line && item.end_line) {
      return [{ start_line: item.start_line, end_line: item.end_line }];
    }
    return [];
  };

  const formatHighlightLabel = (ranges: { start_line: number; end_line: number }[]) => {
    if (!ranges.length) return "未标注重点行";
    const parts = ranges.map((range) => `${range.start_line}-${range.end_line}`);
    return `重点行：${parts.join(", ")}`;
  };

  const showVideo = videoEvidenceList.length > 0;
  const displayGraph = useMemo(
    () => convertKnowledgeGraphToRepoGraph(activeKnowledgeGraph || knowledgeGraph) || repoGraph,
    [activeKnowledgeGraph, knowledgeGraph, repoGraph]
  );
  const showGraph = Boolean(displayGraph?.nodes?.length);
  const showSpotlights = spotlightItems.length > 0;
  const showPoster = Boolean(posterFile && posterEvidence);
  const showPipeline = Boolean(pipelineFile && pipelineEvidence);

  const productStoryNotice = useMemo(() => {
    const source = productStory?.meta?.source;
    const reasonCode = String(productStory?.meta?.reason_code || "").toUpperCase();
    const errorText = String(productStory?.meta?.error || "");
    if (source === "fallback") {
      if (reasonCode === "OPENAI_API_KEY_MISSING" || errorText.includes("OPENAI_API_KEY")) {
        return "未配置 OPENAI_API_KEY，产品叙事使用兜底模板。";
      }
      if (reasonCode === "RATE_LIMIT") {
        return "LLM 触发速率限制，产品叙事已切换兜底模板。";
      }
      if (reasonCode === "AUTH_FAILED") {
        return "LLM 鉴权失败，产品叙事已切换兜底模板。";
      }
      if (reasonCode === "TIMEOUT") {
        return "LLM 请求超时，产品叙事已切换兜底模板。";
      }
      if (reasonCode === "INVALID_RESPONSE") {
        return "LLM 返回格式异常，产品叙事已切换兜底模板。";
      }
      return "产品叙事使用兜底模板（LLM 不可用或返回异常）。";
    }
    return null;
  }, [productStory]);

  const showProductStory =
    productStoryLoading || productStoryError || Boolean(productStoryNotice) || productStoryHasEvidence(productStory);
  const hasVisualSections = showVideo || showGraph || showSpotlights || showPoster || showPipeline;

  const evidenceHints = useMemo(() => {
    if (!visuals) return [];
    const hints = new Set<string>();
    const failedAssets = visuals.assets.filter((asset) => asset.status === "FAILED");
    failedAssets.forEach((asset) => {
      const msg = resolveErrorHint(asset.error_code, {});
      if (msg) {
        hints.add(msg);
      } else if (asset.kind) {
        hints.add(`${asset.kind} 未能生成。`);
      }
    });
    if (!displayGraph?.nodes || displayGraph.nodes.length === 0) {
      hints.add("仓库结构图缺失或为空。");
    }
    if (!spotlightItems.length) {
      hints.add("没有可定位的代码片段（缺少行号证据）。");
    }
    if (!mergedEvidenceCatalog.length) {
      hints.add("证据目录为空，无法渲染可视化内容。");
    }
    if (!repoIndex?.readme_summary?.text) {
      hints.add("未识别到 README 内容，叙事类解释受限。");
    }
    if (ingestMeta?.repo_meta_available === false) {
      hints.add("仓库元数据不可用（可能是 GitHub 访问限制或速率限制）。");
    }
    return Array.from(hints);
  }, [
    visuals,
    displayGraph,
    spotlightItems.length,
    mergedEvidenceCatalog.length,
    repoIndex,
    ingestMeta,
  ]);

  const knowledgeSeedText = useMemo(() => {
    const blocks: string[] = [];
    const readmeSummary = repoIndex?.readme_summary?.text?.trim();
    if (readmeSummary) {
      blocks.push(`# README 摘要\n${readmeSummary}`);
    }
    if (knowledgeAnalysis?.summary) {
      blocks.push(`## 图谱分析\n${knowledgeAnalysis.summary}`);
    }
    if (knowledgeAnalysis?.key_findings?.length) {
      blocks.push(`## 关键发现\n${knowledgeAnalysis.key_findings.map((item) => `- ${item}`).join("\n")}`);
    }
    const spotlightSummary = (spotlights?.items || [])
      .slice(0, 8)
      .map((item) => {
        const title = item?.file_path || "代码片段";
        const explain = item?.explanation?.trim();
        if (!explain) return "";
        return `- ${title}: ${explain}`;
      })
      .filter(Boolean);
    if (spotlightSummary.length) {
      blocks.push(`## 代码片段说明\n${spotlightSummary.join("\n")}`);
    }
    return blocks.join("\n\n");
  }, [repoIndex, spotlights, knowledgeAnalysis]);

  const progressEntries = useMemo(() => {
    const prefixes = [
      "[clone]",
      "[clone-post]",
      "[preflight]",
      "[compose]",
      "[showcase]",
      "[ingest]",
      "[visualize]",
      "VISUALIZE_",
      "ANALYZE_",
      "Manual generation",
      "Checked out commit",
    ];
    const trimmed: LogEntry[] = [];
    const seen = new Set<string>();
    for (const entry of logEntries) {
      const rawLine = String(entry.line || "").trim();
      if (!rawLine) continue;
      const isError = entry.level === "ERROR" || rawLine.includes("ERROR");
      const isRelevant = prefixes.some((prefix) => rawLine.startsWith(prefix));
      if (!isError && !isRelevant) continue;
      const key = `${entry.stream}:${rawLine}`;
      if (seen.has(key)) continue;
      seen.add(key);
      trimmed.push(entry);
    }
    return trimmed;
  }, [logEntries]);

  const renderLogLine = (entry: LogEntry) => {
    const stream = entry.stream || "system";
    const label = STREAM_LABELS[stream] || "进度";
    const rawLine = String(entry.line || "").trim();
    const errorMatch = rawLine.match(/ERROR\s*\[([A-Z0-9_]+)\]/);
    if (errorMatch) {
      const code = errorMatch[1];
      const friendly = resolveErrorHint(code, {}) || "出现问题，但仍会展示可用内容。";
      return { label, text: friendly };
    }
    return { label, text: rawLine || "处理中…" };
  };

  const manualToc = useMemo(() => extractHeadings(manualData?.manual_markdown || ""), [manualData?.manual_markdown]);

  return (
    <div className="visuals" data-testid="panel-visuals">
      {normalizedStatus === "FAILED" ? (
        <div className="card error-card">
          <div className="section-title">出现了一点问题</div>
          <div className="error-message">{displayErrorHint || "我们仍会展示可用内容。"}</div>
        </div>
      ) : null}

      {loading ? (
        <div className="card empty-state">
          <div className="empty-title">讲解内容加载中</div>
          <div className="empty-sub">正在拉取讲解内容。</div>
        </div>
      ) : null}

      <div className="card progress-card" role="tablist" aria-label="detail-tabs">
        <div className="progress-actions">
          <button
            className={`ghost ${activeTab === "logs" ? "active" : ""}`}
            type="button"
            onClick={() => setActiveTab("logs")}
            data-testid="tab-logs"
          >
            进度
          </button>
          <button
            className={`ghost ${activeTab === "manual" ? "active" : ""}`}
            type="button"
            onClick={() => setActiveTab("manual")}
            data-testid="tab-manual"
          >
            说明书
          </button>
        </div>
      </div>

      <div data-testid="panel-logs" hidden={activeTab !== "logs"}>
        <div className="card progress-card">
          <div className="progress-head">
            <div>
              <div className="section-title">进度详情</div>
              <div className="muted">只展示可读进度，不包含敏感信息。</div>
            </div>
            <div className="progress-actions">
              <button className="ghost" type="button" onClick={() => setLogsOpen((v) => !v)}>
                {logsOpen ? "收起进度" : "查看进度"}
              </button>
              {logsOpen ? (
                <button className="ghost" type="button" onClick={fetchLogs} disabled={logsLoading}>
                  {logsLoading ? "刷新中…" : "刷新"}
                </button>
              ) : null}
            </div>
          </div>
          {logsOpen ? (
            <div className="progress-body">
              {logsError ? <div className="error-banner">{logsError}</div> : null}
              {logsLoading && !logEntries.length ? (
                <div className="muted">正在加载进度…</div>
              ) : progressEntries.length ? (
                <div className="progress-list">
                  {progressEntries.map((entry, index) => {
                    const rendered = renderLogLine(entry);
                    if (!rendered.text) return null;
                    return (
                      <div key={`log-${index}`} className="progress-line">
                        <span className="progress-label">{rendered.label}</span>
                        <span className="progress-text">{rendered.text}</span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="muted">暂无可读进度信息。</div>
              )}
            </div>
          ) : null}
        </div>
      </div>

      <div data-testid="panel-manual" hidden={activeTab !== "manual"}>
        <div className="card progress-card">
          <div className="section-title">说明书目录</div>
          {manualLoading ? (
            <div className="muted">说明书加载中…</div>
          ) : manualError ? (
            <div className="error-banner">{manualError}</div>
          ) : manualToc.length ? (
            <ul className="doc-toc" data-testid="manual-toc">
              {manualToc.map((item) => (
                <li key={item.id} className={`toc-level-${item.level}`}>
                  <a href={`#${item.id}`}>{item.text}</a>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted">暂无目录。</div>
          )}
        </div>
      </div>

      <div className={`visuals-layout${showProductStory ? "" : " visuals-layout--single"}`}>
        <div className="visuals-main">
          {visuals ? (
            <>
              {normalizedStatus === "PARTIAL" ? (
                <div className="card info-card">
                  仓库规模较大，已自动聚焦最关键的部分进行讲解。
                </div>
              ) : null}
              {hasVisualSections ? (
                <div className="visual-pack">
                  {showVideo ? (
                    <div className="card visual-pack-video">
                      <div className="visual-pack-head">
                        <div>
                          <div className="section-title">这个仓库在干什么？</div>
                          <div className="muted">基于证据的可视化讲解</div>
                        </div>
                        <EvidencePanel evidences={videoEvidenceList} />
                      </div>
                      {videoFile ? (
                        <video
                          key={videoFile.name}
                          src={assetUrl(videoFile.url)}
                          controls
                          className="visual-media"
                        />
                      ) : (
                        <div className="muted">视频生成失败，但证据仍可用于其他展示。</div>
                      )}
                    </div>
                  ) : null}

                  {showGraph ? (
                    <div className="card visual-pack-graph">
                      <div className="visual-pack-head">
                        <div>
                          <div className="section-title">仓库结构与模块关系</div>
                          <div className="muted">统一后端图谱资产，支持结构关系与逻辑分析联动</div>
                        </div>
                        {graphEvidence ? <EvidencePanel evidence={graphEvidence} /> : null}
                      </div>
                      {displayGraph?.nodes?.length ? (
                        <>
                          <GraphViewer graph={displayGraph} />
                          {knowledgeAnalysis?.summary || knowledgeAnalysis?.key_findings?.length ? (
                            <div className="graph-analysis">
                              {knowledgeAnalysis?.summary ? (
                                <div className="graph-analysis-summary">{knowledgeAnalysis.summary}</div>
                              ) : null}
                              {knowledgeAnalysis?.key_findings?.length ? (
                                <ul className="graph-analysis-list">
                                  {knowledgeAnalysis.key_findings.slice(0, 4).map((item, index) => (
                                    <li key={`graph-finding-${index}`}>{item}</li>
                                  ))}
                                </ul>
                              ) : null}
                            </div>
                          ) : null}
                        </>
                      ) : (
                        <div className="muted">暂无结构图数据。</div>
                      )}
                    </div>
                  ) : null}

                  {showPoster ? (
                    <div className="card visual-pack-poster">
                      <div className="visual-pack-head">
                        <div>
                          <div className="section-title">结构概览</div>
                          <div className="muted">基于结构证据生成的可视化图</div>
                        </div>
                        {posterEvidence ? <EvidencePanel evidence={posterEvidence} /> : null}
                      </div>
                      {posterFallback ? <div className="badge badge--warning">参考图</div> : null}
                      {posterNotice ? <div className="muted poster-note">{posterNotice}</div> : null}
                      {posterFile ? (
                        <img
                          className="visual-media visual-image"
                          src={assetUrl(posterFile.url)}
                          alt="仓库结构概览"
                        />
                      ) : (
                        <div className="muted">结构概览未生成。</div>
                      )}
                    </div>
                  ) : null}

                  {showPipeline ? (
                    <div className="card visual-pack-pipeline">
                      <div className="visual-pack-head">
                        <div>
                          <div className="section-title">工作流程概览</div>
                          <div className="muted">从证据推导的流程关系图</div>
                        </div>
                        {pipelineEvidence ? <EvidencePanel evidence={pipelineEvidence} /> : null}
                      </div>
                      {pipelineFile ? (
                        <img
                          className="visual-media visual-image"
                          src={assetUrl(pipelineFile.url)}
                          alt="流程概览"
                        />
                      ) : (
                        <div className="muted">流程概览未生成。</div>
                      )}
                    </div>
                  ) : null}

                  {showSpotlights ? (
                    <div className="card visual-pack-spotlights">
                      <div className="visual-pack-head">
                        <div>
                          <div className="section-title">核心代码解读</div>
                          <div className="muted">每段片段都附带可追溯证据</div>
                        </div>
                      </div>
                      <div className="spotlight-grid">
                        {spotlightItems.map((item, index) => {
                          const snippet = item.snippet || "";
                          const lines = snippet.split("\n");
                          const startLine = item.start_line || 1;
                          const highlightRanges = highlightRangesFor(item);
                          const highlightLabel = formatHighlightLabel(highlightRanges);
                          const explanationText = item.explanation?.trim();
                          return (
                            <div key={`${item.file_path}-${index}`} className="spotlight-card">
                              <div className="spotlight-head">
                                <span className="spotlight-path">{item.file_path}</span>
                                <span className="badge badge--working">{item.language || "代码"}</span>
                              </div>
                              <div className="spotlight-range">{highlightLabel}</div>
                              {explanationText ? <div className="spotlight-explain">{explanationText}</div> : null}
                              {item.evidence ? <EvidencePanel evidence={item.evidence} /> : null}
                              <div className="spotlight-code" role="region" aria-label={`${item.file_path} 代码片段`}>
                                <code>
                                  {lines.map((line, lineIndex) => {
                                    const lineNumber = startLine + lineIndex;
                                    const isHighlight = highlightRanges.some(
                                      (range) => lineNumber >= range.start_line && lineNumber <= range.end_line
                                    );
                                    return (
                                      <div
                                        key={`${item.file_path}-line-${lineNumber}`}
                                        className={`code-line${isHighlight ? " is-highlight" : ""}`}
                                      >
                                        <span className="code-line-number">{lineNumber}</span>
                                        <span className="code-line-text">{line || " "}</span>
                                      </div>
                                    );
                                  })}
                                </code>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="card empty-state">
                  <div className="empty-title">暂无可展示的证据内容</div>
                  <div className="empty-sub">当前仓库缺少可用于生成可视化的证据。</div>
                  {evidenceHints.length ? (
                    <div className="empty-hints">
                      {evidenceHints.map((hint, index) => (
                        <div key={`hint-${index}`} className="empty-hint">
                          {hint}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
            </>
          ) : normalizedStatus === "PENDING" || normalizedStatus === "RUNNING" ? (
            <div className="card empty-state">
              <div className="empty-title">讲解内容生成中</div>
              <div className="empty-sub">正在准备结果，请稍候。</div>
            </div>
          ) : (
            <div className="card empty-state">
              <div className="empty-title">还没有讲解内容</div>
              <div className="empty-sub">提交仓库后会自动生成讲解内容。</div>
            </div>
          )}
        </div>
        {showProductStory ? (
          <div className="visuals-side">
            <ProductStoryPanel
              story={productStory}
              loading={productStoryLoading}
              error={productStoryError}
              notice={productStoryNotice}
            />
          </div>
        ) : null}
      </div>

      <KnowledgeGraphWorkbench
        caseId={caseId}
        apiBase={API_BASE}
        seedText={knowledgeSeedText}
        initialGraph={activeKnowledgeGraph || knowledgeGraph}
        analysis={knowledgeAnalysis}
        onPublishGraph={(next) => setActiveKnowledgeGraph(next)}
        pushToast={pushToast}
      />
    </div>
  );
}

function ProductStoryPanel({
  story,
  loading,
  error,
  notice,
}: {
  story?: ProductStory | null;
  loading?: boolean;
  error?: string | null;
  notice?: string | null;
}) {
  const [storyCollapsed, setStoryCollapsed] = useState(false);

  useEffect(() => {
    setStoryCollapsed(false);
  }, [story]);

  const renderClaim = (claim?: EvidenceClaim | null, key?: string) => {
    if (!claim || !isEvidenceValid(claim.evidence)) return null;
    return (
      <div key={key} className="story-claim">
        <div className="story-claim-text">
          <LongText text={claim.claim} lines={3} allowToggle className="story-copy" />
        </div>
        <EvidencePanel evidence={claim.evidence} defaultOpen={false} />
      </div>
    );
  };

  const hookHeadline = story?.hook?.headline || null;
  const hookSubline = story?.hook?.subline || null;
  const problemTarget = story?.problem_context?.target_user || null;
  const problemPain = story?.problem_context?.pain_point || null;
  const problemBad = story?.problem_context?.current_bad_solution || null;
  const outcomes = (story?.what_this_repo_gives_you || []).filter(
    (item) => item && isEvidenceValid(item.evidence)
  );
  const scenarios = (story?.usage_scenarios || []).filter(
    (item) => item && isEvidenceValid(item.evidence)
  );
  const whyNow = story?.why_it_matters_now || null;
  const nextBuilder = story?.next_step_guidance?.if_you_are_a_builder || null;
  const nextPm = story?.next_step_guidance?.if_you_are_a_pm_or_founder || null;
  const nextEval = story?.next_step_guidance?.if_you_are_evaluating || null;

  const hasHook =
    Boolean(hookHeadline && isEvidenceValid(hookHeadline.evidence)) ||
    Boolean(hookSubline && isEvidenceValid(hookSubline.evidence));
  const hasProblem =
    Boolean(problemTarget && isEvidenceValid(problemTarget.evidence)) ||
    Boolean(problemPain && isEvidenceValid(problemPain.evidence)) ||
    Boolean(problemBad && isEvidenceValid(problemBad.evidence));
  const hasOutcomes = outcomes.length > 0;
  const hasScenarios = scenarios.length > 0;
  const hasWhy = Boolean(whyNow && isEvidenceValid(whyNow.evidence));
  const hasNext =
    Boolean(nextBuilder && isEvidenceValid(nextBuilder.evidence)) ||
    Boolean(nextPm && isEvidenceValid(nextPm.evidence)) ||
    Boolean(nextEval && isEvidenceValid(nextEval.evidence));
  const hasStory = hasHook || hasProblem || hasOutcomes || hasScenarios || hasWhy || hasNext;
  return (
    <aside className="product-story-panel" data-testid="product-story-panel">
      {notice ? <div className="card story-meta">{notice}</div> : null}
      {hasStory ? (
        <>
          <div className="story-panel-head">
            <div className="section-title">产品叙事</div>
            <button className="ghost" type="button" onClick={() => setStoryCollapsed((value) => !value)}>
              {storyCollapsed ? "展开叙事" : "收起叙事"}
            </button>
          </div>
          {storyCollapsed ? (
            <div className="card story-empty">
              <div className="muted">产品叙事已收起，点击“展开叙事”查看详情。</div>
            </div>
          ) : (
            <>
              {hasHook ? (
                <div className="card story-hero">
                  <div className="story-label">产品叙事</div>
                  {renderClaim(hookHeadline, "hook-headline")}
                  {renderClaim(hookSubline, "hook-subline")}
                </div>
              ) : null}

              {hasProblem ? (
                <div className="card story-section">
                  <div className="section-title">这是谁需要的？</div>
                  {renderClaim(problemTarget, "problem-target")}
                  {(problemPain && isEvidenceValid(problemPain.evidence)) ||
                  (problemBad && isEvidenceValid(problemBad.evidence)) ? (
                    <div className="story-divider" />
                  ) : null}
                  {problemPain && isEvidenceValid(problemPain.evidence) ? (
                    <div className="story-callout">
                      <span>痛点</span>
                      <LongText text={problemPain.claim} lines={3} allowToggle className="story-copy" />
                      <EvidencePanel evidence={problemPain.evidence} defaultOpen={false} />
                    </div>
                  ) : null}
                  {problemBad && isEvidenceValid(problemBad.evidence) ? (
                    <div className="story-callout">
                      <span>现状</span>
                      <LongText text={problemBad.claim} lines={3} allowToggle className="story-copy" />
                      <EvidencePanel evidence={problemBad.evidence} defaultOpen={false} />
                    </div>
                  ) : null}
                </div>
              ) : null}

              {hasOutcomes ? (
                <div className="card story-section">
                  <div className="section-title">它能带来的结果</div>
                  <div className="story-outcomes">
                    {outcomes.slice(0, 3).map((item, index) => (
                      <div key={`outcome-${index}`} className="story-outcome">
                        <div className="story-outcome-title">
                          <LongText text={item.claim} lines={3} allowToggle />
                        </div>
                        <EvidencePanel evidence={item.evidence} defaultOpen={false} />
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              {hasScenarios ? (
                <div className="card story-section">
                  <div className="section-title">适用场景</div>
                  <ul className="story-list">
                    {scenarios.slice(0, 3).map((item, index) => (
                      <li key={`scenario-${index}`}>
                        <LongText text={item.claim} lines={3} allowToggle />
                        <EvidencePanel evidence={item.evidence} defaultOpen={false} />
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {hasWhy ? (
                <div className="card story-section">
                  <div className="section-title">为什么现在值得关注</div>
                  {renderClaim(whyNow, "why-now")}
                </div>
              ) : null}

              {hasNext ? (
                <div className="card story-section">
                  <div className="section-title">下一步怎么做</div>
                  <div className="story-next">
                    {nextBuilder && isEvidenceValid(nextBuilder.evidence) ? (
                      <div>
                        <div className="story-next-label">如果你是构建者</div>
                        {renderClaim(nextBuilder, "next-builder")}
                      </div>
                    ) : null}
                    {nextPm && isEvidenceValid(nextPm.evidence) ? (
                      <div>
                        <div className="story-next-label">如果你是产品或业务负责人</div>
                        {renderClaim(nextPm, "next-pm")}
                      </div>
                    ) : null}
                    {nextEval && isEvidenceValid(nextEval.evidence) ? (
                      <div>
                        <div className="story-next-label">如果你在评估是否要继续</div>
                        {renderClaim(nextEval, "next-eval")}
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </>
          )}
        </>
      ) : (
        <div className="card story-empty">
          <div className="section-title">产品叙事</div>
          <div className="muted">
            {loading ? "正在生成产品叙事…" : error ? error : "产品叙事尚未生成。"}
          </div>
        </div>
      )}
    </aside>
  );
}

function convertKnowledgeGraphToRepoGraph(graph?: KnowledgeGraph | null): RepoGraph | null {
  if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) return null;
  return {
    nodes: graph.nodes.map((node) => ({
      id: node.id,
      label: node.label,
      x: node.x,
      y: node.y,
      type: node.type,
    })),
    edges: (graph.edges || []).map((edge) => ({
      source: edge.source,
      target: edge.target,
      relation: edge.relation,
      weight: edge.weight,
    })),
    meta: {
      truncated: false,
    },
  };
}

function graphNodeTypeLabel(value?: string | null) {
  if (!value) return "节点";
  const normalized = value.toLowerCase();
  if (normalized === "dir") return "目录";
  if (normalized === "root") return "根目录";
  if (normalized === "file") return "文件";
  if (normalized === "module") return "模块";
  if (normalized === "service") return "服务";
  if (normalized === "concept") return "概念";
  if (normalized === "document") return "文档";
  if (normalized === "data") return "数据";
  if (normalized === "process") return "流程";
  if (normalized === "package") return "包";
  return "节点";
}

function GraphViewer({ graph }: { graph: RepoGraph }) {
  type GraphNode = NonNullable<RepoGraph["nodes"]>[number];
  type GraphEdge = NonNullable<RepoGraph["edges"]>[number];
  type DragState = {
    startClientX: number;
    startClientY: number;
    startOffsetX: number;
    startOffsetY: number;
  };

  const nodes: GraphNode[] = Array.isArray(graph?.nodes)
    ? graph.nodes.filter((item): item is GraphNode => Boolean(item && item.id))
    : [];
  const edges: GraphEdge[] = Array.isArray(graph?.edges)
    ? graph.edges.filter((item): item is GraphEdge => Boolean(item && item.source && item.target))
    : [];

  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeKey, setSelectedEdgeKey] = useState<string | null>(null);
  const [searchText, setSearchText] = useState("");
  const [nodeTypeFilter, setNodeTypeFilter] = useState("all");
  const [relationFilter, setRelationFilter] = useState("all");
  const [focusSelected, setFocusSelected] = useState(false);
  const [showAllLabels, setShowAllLabels] = useState(false);
  const width = 1200;
  const height = 700;
  const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
  const normalizedSearch = searchText.trim().toLowerCase();

  const edgeKeyFor = useCallback(
    (edge: GraphEdge) => `${edge.source}>>${edge.target}>>${edge.relation || "关联"}`,
    []
  );

  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    nodes.forEach((node) => map.set(node.id, node));
    return map;
  }, [nodes]);

  const nodeTypeStats = useMemo(() => {
    const map = new Map<string, { label: string; count: number }>();
    nodes.forEach((node) => {
      const key = String(node.type || "concept").toLowerCase();
      const existing = map.get(key);
      if (existing) {
        existing.count += 1;
        return;
      }
      map.set(key, { label: graphNodeTypeLabel(key), count: 1 });
    });
    return Array.from(map.entries()).sort((a, b) => b[1].count - a[1].count);
  }, [nodes]);

  const relationStats = useMemo(() => {
    const map = new Map<string, number>();
    edges.forEach((edge) => {
      const key = edge.relation || "关联";
      map.set(key, (map.get(key) || 0) + 1);
    });
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1]);
  }, [edges]);

  const edgesByRelation = useMemo(
    () => edges.filter((edge) => relationFilter === "all" || (edge.relation || "关联") === relationFilter),
    [edges, relationFilter]
  );

  const baseNodeIds = useMemo(() => {
    const ids = new Set<string>();
    nodes.forEach((node) => {
      const normalizedType = String(node.type || "concept").toLowerCase();
      if (nodeTypeFilter !== "all" && normalizedType !== nodeTypeFilter) return;
      const label = String(node.label || node.id).toLowerCase();
      if (normalizedSearch && !label.includes(normalizedSearch)) return;
      ids.add(node.id);
    });
    return ids;
  }, [nodes, nodeTypeFilter, normalizedSearch]);

  const contextualNodeIds = useMemo(() => {
    const ids = new Set<string>(baseNodeIds);
    edgesByRelation.forEach((edge) => {
      if (ids.has(edge.source) || ids.has(edge.target)) {
        ids.add(edge.source);
        ids.add(edge.target);
      }
    });
    return ids;
  }, [baseNodeIds, edgesByRelation]);

  const visibleNodeIds = useMemo(() => {
    if (!focusSelected || !selectedNodeId) return contextualNodeIds;
    const focused = new Set<string>();
    if (contextualNodeIds.has(selectedNodeId)) {
      focused.add(selectedNodeId);
    }
    edgesByRelation.forEach((edge) => {
      if (edge.source === selectedNodeId || edge.target === selectedNodeId) {
        if (contextualNodeIds.has(edge.source)) focused.add(edge.source);
        if (contextualNodeIds.has(edge.target)) focused.add(edge.target);
      }
    });
    return focused;
  }, [contextualNodeIds, edgesByRelation, focusSelected, selectedNodeId]);

  const visibleNodes = useMemo(
    () => nodes.filter((node) => visibleNodeIds.has(node.id)),
    [nodes, visibleNodeIds]
  );

  const visibleEdges = useMemo(
    () =>
      edgesByRelation.filter((edge) => {
        if (!visibleNodeIds.has(edge.source) || !visibleNodeIds.has(edge.target)) return false;
        if (focusSelected && selectedNodeId) {
          return edge.source === selectedNodeId || edge.target === selectedNodeId;
        }
        return true;
      }),
    [edgesByRelation, visibleNodeIds, focusSelected, selectedNodeId]
  );

  const degreeByNodeId = useMemo(() => {
    const map = new Map<string, number>();
    visibleNodes.forEach((node) => map.set(node.id, 0));
    visibleEdges.forEach((edge) => {
      map.set(edge.source, (map.get(edge.source) || 0) + Number(edge.weight || 1));
      map.set(edge.target, (map.get(edge.target) || 0) + Number(edge.weight || 1));
    });
    return map;
  }, [visibleNodes, visibleEdges]);

  const selectedNode = useMemo(
    () => (selectedNodeId ? nodeById.get(selectedNodeId) || null : null),
    [nodeById, selectedNodeId]
  );

  const selectedEdge = useMemo(() => {
    if (!selectedEdgeKey) return null;
    return visibleEdges.find((edge) => edgeKeyFor(edge) === selectedEdgeKey) || null;
  }, [visibleEdges, selectedEdgeKey, edgeKeyFor]);

  const neighborNodeIds = useMemo(() => {
    const ids = new Set<string>();
    if (!selectedNodeId) return ids;
    visibleEdges.forEach((edge) => {
      if (edge.source === selectedNodeId) ids.add(edge.target);
      if (edge.target === selectedNodeId) ids.add(edge.source);
    });
    return ids;
  }, [selectedNodeId, visibleEdges]);

  const showLabels = showAllLabels || visibleNodes.length <= 52 || zoom >= 1.2;

  useEffect(() => {
    if (selectedNodeId && !visibleNodeIds.has(selectedNodeId)) {
      setSelectedNodeId(null);
    }
  }, [selectedNodeId, visibleNodeIds]);

  useEffect(() => {
    if (!selectedEdgeKey) return;
    if (!visibleEdges.some((edge) => edgeKeyFor(edge) === selectedEdgeKey)) {
      setSelectedEdgeKey(null);
    }
  }, [selectedEdgeKey, visibleEdges, edgeKeyFor]);

  useEffect(() => {
    const stopDrag = () => setDragState(null);
    window.addEventListener("mouseup", stopDrag);
    return () => window.removeEventListener("mouseup", stopDrag);
  }, []);

  const handleCanvasMouseDown = (event: React.MouseEvent<SVGSVGElement>) => {
    if (event.button !== 0) return;
    setDragState({
      startClientX: event.clientX,
      startClientY: event.clientY,
      startOffsetX: offset.x,
      startOffsetY: offset.y,
    });
  };

  const handleCanvasMouseMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!dragState) return;
    const dx = (event.clientX - dragState.startClientX) / zoom;
    const dy = (event.clientY - dragState.startClientY) / zoom;
    setOffset({
      x: dragState.startOffsetX + dx,
      y: dragState.startOffsetY + dy,
    });
  };

  const handleCanvasWheel = (event: React.WheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.08 : 0.92;
    setZoom((value) => clamp(value * factor, 0.55, 2.8));
  };

  const resetView = () => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  };

  const normalizeNodeClass = (type?: string | null) => {
    const value = String(type || "").toLowerCase();
    if (value === "dir" || value === "root") return "graph-node--dir";
    if (value === "module") return "graph-node--module";
    if (value === "service") return "graph-node--service";
    if (value === "data") return "graph-node--data";
    if (value === "document") return "graph-node--document";
    if (value === "process") return "graph-node--process";
    if (value === "concept") return "graph-node--concept";
    return "graph-node--file";
  };

  return (
    <div className="graph-viewer">
      <div className="graph-controls">
        <div className="graph-control-group graph-control-group--search">
          <input
            className="graph-search-input"
            type="search"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="搜索节点（名称）"
          />
        </div>
        <div className="graph-control-group">
          <select className="graph-select" value={nodeTypeFilter} onChange={(event) => setNodeTypeFilter(event.target.value)}>
            <option value="all">全部类型</option>
            {nodeTypeStats.map(([key, item]) => (
              <option key={`type-${key}`} value={key}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select className="graph-select" value={relationFilter} onChange={(event) => setRelationFilter(event.target.value)}>
            <option value="all">全部关系</option>
            {relationStats.map(([relation, count]) => (
              <option key={`rel-${relation}`} value={relation}>
                {relation} ({count})
              </option>
            ))}
          </select>
          <label className="graph-check">
            <input
              type="checkbox"
              checked={focusSelected}
              onChange={(event) => setFocusSelected(event.target.checked)}
            />
            聚焦选中节点
          </label>
          <label className="graph-check">
            <input
              type="checkbox"
              checked={showAllLabels}
              onChange={(event) => setShowAllLabels(event.target.checked)}
            />
            全部标签
          </label>
        </div>
        <div className="graph-control-group graph-control-group--actions">
          <button className="ghost" type="button" onClick={() => setZoom((z) => clamp(z + 0.2, 0.55, 2.8))}>
            放大
          </button>
          <button className="ghost" type="button" onClick={() => setZoom((z) => clamp(z - 0.2, 0.55, 2.8))}>
            缩小
          </button>
          <button className="ghost" type="button" onClick={resetView}>
            重置视角
          </button>
          <button
            className="ghost"
            type="button"
            onClick={() => {
              setSelectedNodeId(null);
              setSelectedEdgeKey(null);
            }}
          >
            清空选中
          </button>
        </div>
      </div>
      <div className="graph-meta-line">
        <span className="graph-meta-chip">显示节点 {visibleNodes.length} / {nodes.length}</span>
        <span className="graph-meta-chip">显示关系 {visibleEdges.length} / {edges.length}</span>
        <span className="graph-meta-chip">缩放 {Math.round(zoom * 100)}%</span>
      </div>
      <div className={`graph-canvas${dragState ? " is-dragging" : ""}`}>
        <svg
          viewBox={`${-width / 2} ${-height / 2} ${width} ${height}`}
          className="graph-svg"
          onMouseDown={handleCanvasMouseDown}
          onMouseMove={handleCanvasMouseMove}
          onMouseUp={() => setDragState(null)}
          onMouseLeave={() => setDragState(null)}
          onWheel={handleCanvasWheel}
          onClick={() => {
            setSelectedNodeId(null);
            setSelectedEdgeKey(null);
          }}
        >
          <g transform={`translate(${offset.x} ${offset.y}) scale(${zoom})`}>
            {visibleEdges.map((edge, index) => {
              const source = nodeById.get(edge.source);
              const target = nodeById.get(edge.target);
              if (!source || !target) return null;
              const relation = edge.relation || "关联";
              const edgeKey = edgeKeyFor(edge);
              const selected = selectedEdgeKey === edgeKey;
              const selectedRelated = selectedNodeId ? edge.source === selectedNodeId || edge.target === selectedNodeId : true;
              const labelX = ((source.x || 0) + (target.x || 0)) / 2;
              const labelY = ((source.y || 0) + (target.y || 0)) / 2;
              return (
                <g key={`${edge.source}-${edge.target}-${index}`}>
                  <line
                    x1={source.x || 0}
                    y1={source.y || 0}
                    x2={target.x || 0}
                    y2={target.y || 0}
                    className={`graph-edge${selected ? " is-selected" : ""}${selectedRelated ? "" : " is-dim"}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedEdgeKey(edgeKey);
                      setSelectedNodeId(null);
                    }}
                  />
                  {(showLabels || selected) && relation ? (
                    <text
                      x={labelX}
                      y={labelY - 4}
                      className={`graph-edge-label${selected ? " is-selected" : ""}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelectedEdgeKey(edgeKey);
                        setSelectedNodeId(null);
                      }}
                    >
                      {relation}
                    </text>
                  ) : null}
                </g>
              );
            })}
            {visibleNodes.map((node) => {
              const normalizedType = String(node.type || "").toLowerCase();
              const isDir = normalizedType === "dir" || normalizedType === "root";
              const selected = selectedNodeId === node.id;
              const related = selectedNodeId ? selected || neighborNodeIds.has(node.id) : true;
              const label = String(node.label || node.id);
              const isSearchMatch = normalizedSearch ? label.toLowerCase().includes(normalizedSearch) : false;
              const degree = degreeByNodeId.get(node.id) || 0;
              const radius = clamp((isDir ? 13 : 10.5) + degree * 0.4, 10, 24);
              return (
                <g
                  key={node.id}
                  transform={`translate(${node.x || 0}, ${node.y || 0})`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setSelectedNodeId(node.id);
                    setSelectedEdgeKey(null);
                  }}
                >
                  <circle
                    r={radius}
                    className={`graph-node ${normalizeNodeClass(node.type)}${selected ? " is-selected" : ""}${
                      related ? "" : " is-dim"
                    }${isSearchMatch ? " is-match" : ""}`}
                  />
                  {showLabels || selected ? (
                    <text className="graph-label" x={radius + 6} y={4}>
                      {label}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>
        </svg>
      </div>
      <div className="graph-legend">
        {nodeTypeStats.map(([key, item]) => (
          <div className="graph-legend-item" key={`legend-${key}`}>
            <span className={`graph-legend-dot ${normalizeNodeClass(key)}`} />
            <span>{item.label}</span>
            <strong>{item.count}</strong>
          </div>
        ))}
      </div>
      {selectedNode || selectedEdge ? (
        <div className="graph-tooltip">
          {selectedNode ? (
            <>
              <div className="graph-tooltip-title">{selectedNode.label || selectedNode.id}</div>
              <div className="muted">{graphNodeTypeLabel(selectedNode.type)} · 关联 {degreeByNodeId.get(selectedNode.id) || 0}</div>
            </>
          ) : null}
          {selectedEdge ? (
            <>
              <div className="graph-tooltip-title">关系: {selectedEdge.relation || "关联"}</div>
              <div className="muted">
                {nodeById.get(selectedEdge.source)?.label || selectedEdge.source} {"->"}{" "}
                {nodeById.get(selectedEdge.target)?.label || selectedEdge.target}
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function MarkdownRenderer({ markdown }: { markdown: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const html = useMemo(() => {
    const renderer = new marked.Renderer();
    const counts: Record<string, number> = {};
    renderer.heading = (text, level, raw) => {
      const base = slugifyHeading(raw || text);
      const count = (counts[base] ?? 0) + 1;
      counts[base] = count;
      const id = count > 1 ? `${base}-${count}` : base;
      return `<h${level} id="${id}">${text}</h${level}>`;
    };
    return marked.parse(markdown, { breaks: true, renderer });
  }, [markdown]);

  useEffect(() => {
    if (!containerRef.current) return;
    const container = containerRef.current;
    const blocks = container.querySelectorAll("pre code.language-mermaid, pre code.lang-mermaid, pre code.mermaid");
    blocks.forEach((block) => {
      const parent = block.parentElement;
      if (!parent) return;
      const wrapper = document.createElement("div");
      wrapper.className = "mermaid";
      wrapper.textContent = block.textContent || "";
      parent.replaceWith(wrapper);
    });
    mermaid.run({ nodes: container.querySelectorAll(".mermaid") });
  }, [html]);

  return <div className="markdown" ref={containerRef} dangerouslySetInnerHTML={{ __html: html }} />;
}

function DocReader({
  markdown,
  meta,
  onAction,
}: {
  markdown: string;
  meta: ManualMeta;
  onAction: (action: DocAction) => void;
}) {
  const toc = useMemo(() => extractHeadings(markdown), [markdown]);
  const sources = useMemo(() => deriveSources(meta), [meta]);
  const template = useMemo(() => buildDocTemplate(toc), [toc]);
  const signalEntries = useMemo(() => Object.entries(meta.signals || {}), [meta.signals]);

  return (
    <div className="doc-reader" data-testid="doc-reader">
      <div className="doc-reader-main">
        <div className="card doc-content">
          <div className="doc-content-head">
            <div>
              <div className="section-title">阅读模式</div>
              <div className="section-sub">结构化说明书 · 溯源可追溯 · 可批注</div>
            </div>
            <div className="doc-actions">
              <button className="ghost" type="button" onClick={() => onAction("ask")} data-testid="doc-action-ask">
                问一问
              </button>
              <button className="ghost" type="button" onClick={() => onAction("note")} data-testid="doc-action-note">
                批注
              </button>
              <button className="ghost" type="button" onClick={() => onAction("share")} data-testid="doc-action-share">
                分享
              </button>
              <button
                className="ghost"
                type="button"
                onClick={() => onAction("sources")}
                data-testid="doc-action-sources"
              >
                溯源
              </button>
            </div>
          </div>
          <MarkdownRenderer markdown={markdown} />
        </div>
      </div>
      <aside className="doc-reader-rail">
        <div className="card doc-panel" data-testid="doc-panel-toc">
          <div className="doc-panel-title">说明书目录</div>
          {toc.length ? (
            <ul className="doc-toc" data-testid="manual-toc">
              {toc.map((item) => (
                <li key={item.id} className={`toc-level-${item.level}`}>
                  <a href={`#${item.id}`}>{item.text}</a>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted">暂无标题，请检查说明书内容。</div>
          )}
        </div>
        <div className="card doc-panel" data-testid="doc-panel-sources">
          <div className="doc-panel-title">来源线索</div>
          {sources.length ? (
            <div className="doc-sources" data-testid="doc-sources">
              {sources.map((source) => (
                <div key={source.label} className={`source-item source-${source.status}`}>
                  <div className="source-label">{source.label}</div>
                  <div className="source-detail">{source.detail}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="muted">暂无来源信息，可重新生成说明书。</div>
          )}
        </div>
        <div className="card doc-panel" data-testid="doc-panel-template">
          <div className="doc-panel-title">模板覆盖</div>
          <div className="doc-template" data-testid="doc-template">
            {template.map((item) => (
              <div key={item.label} className={`template-item ${item.found ? "found" : "missing"}`}>
                <span className="template-dot" />
                <span>{item.label}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card doc-panel" data-testid="doc-panel-meta">
          <div className="doc-panel-title">元信息</div>
          <div className="doc-meta" data-testid="doc-meta">
            <div className="doc-meta-row">
              <span className="meta-label">生成时间</span>
              <span>{formatTime(meta.generated_at)}</span>
            </div>
            <div className="doc-meta-row">
              <span className="meta-label">生成器</span>
              <span>{meta.generator_version}</span>
            </div>
            <div className="doc-meta-row">
              <span className="meta-label">仓库指纹</span>
              <LongText text={meta.repo_fingerprint || "-"} lines={1} allowToggle={false} mono />
            </div>
            <div className="doc-meta-row">
              <span className="meta-label">相似度</span>
              <span>{meta.similarity_score ?? "-"}</span>
            </div>
            <div className="doc-meta-row">
              <span className="meta-label">耗时（毫秒）</span>
              <span>{meta.time_cost_ms}</span>
            </div>
            <div className="doc-meta-row">
              <span className="meta-label">警告</span>
              <LongText text={(meta.warnings && meta.warnings.length) ? meta.warnings.join(", ") : "-"} lines={2} />
            </div>
            <div className="doc-meta-signals" data-testid="doc-signals">
              {signalEntries.map(([key, value]) => {
                const display =
                  typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);
                return (
                  <div key={key} className="signal-item">
                    <span className="signal-key">{key}</span>
                    <LongText text={display} lines={2} allowToggle className="signal-value" />
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}

type ActionItem = {
  label: string;
  disabled?: boolean;
  onClick: () => void;
};

function ActionMenu({ items }: { items: ActionItem[] }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    const handleClick = (event: MouseEvent) => {
      if (!menuRef.current) return;
      if (!menuRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [open]);

  return (
    <div className="action-menu" ref={menuRef}>
      <IconButton
        icon="more"
        label="更多操作"
        onClick={() => setOpen((value) => !value)}
        ariaExpanded={open}
      />
      {open ? (
        <div className="dropdown-menu">
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              className="dropdown-item"
              disabled={item.disabled}
              onClick={() => {
                item.onClick();
                setOpen(false);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

type IconName = "copy" | "check" | "open" | "stop" | "retry" | "more" | "link" | "details";

function Icon({ name, className }: { name: IconName; className?: string }) {
  switch (name) {
    case "copy":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <rect x="9" y="9" width="12" height="12" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      );
    case "check":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <path d="M4 12l5 5 11-11" />
        </svg>
      );
    case "open":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <path d="M14 3h7v7" />
          <path d="M10 14L21 3" />
          <path d="M21 14v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h6" />
        </svg>
      );
    case "stop":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <rect x="6" y="6" width="12" height="12" rx="2" />
        </svg>
      );
    case "retry":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <path d="M3 12a9 9 0 1 0 3-6.7" />
          <path d="M3 4v5h5" />
        </svg>
      );
    case "more":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </svg>
      );
    case "link":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <path d="M10 13a5 5 0 0 1 0-7l2-2a5 5 0 0 1 7 7l-2 2" />
          <path d="M14 11a5 5 0 0 1 0 7l-2 2a5 5 0 1 1-7-7l2-2" />
        </svg>
      );
    case "details":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true" className={className || "icon"}>
          <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
    default:
      return null;
  }
}

function IconButton({
  icon,
  label,
  onClick,
  disabled,
  ariaExpanded,
}: {
  icon: IconName;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  ariaExpanded?: boolean;
}) {
  return (
    <button
      className="icon-button"
      type="button"
      title={label}
      aria-label={label}
      aria-expanded={ariaExpanded}
      onClick={onClick}
      disabled={disabled}
    >
      <Icon name={icon} />
    </button>
  );
}

function CopyButton({
  value,
  label,
  onCopied,
}: {
  value: string;
  label?: string;
  onCopied?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      onCopied?.();
      setTimeout(() => setCopied(false), 1200);
    } catch (err) {
      setCopied(false);
    }
  };

  return (
    <IconButton
      icon={copied ? "check" : "copy"}
      label={copied ? "已复制" : label || "复制"}
      onClick={handleCopy}
    />
  );
}

export function LongText({
  text,
  lines = 2,
  allowToggle = true,
  className,
  mono,
  title,
}: {
  text?: string | null;
  lines?: number;
  allowToggle?: boolean;
  className?: string;
  mono?: boolean;
  title?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const content = text ?? "-";
  const raw = typeof content === "string" ? content : String(content);
  const showToggle = allowToggle && lines > 1 && raw.length > 120;
  const style = { ["--lines" as string]: lines } as React.CSSProperties;
  return (
    <div
      className={`long-text ${expanded ? "expanded" : ""} ${mono ? "mono" : ""} ${className || ""}`}
      style={style}
    >
      <span title={title || raw}>{content}</span>
      {showToggle ? (
        <button className="text-link" type="button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "收起" : "展开"}
        </button>
      ) : null}
    </div>
  );
}

function CopyField({
  label,
  value,
  mono,
  lines = 1,
  allowToggle = false,
  placeholder = "-",
  onCopied,
}: {
  label?: string;
  value?: string | null;
  mono?: boolean;
  lines?: number;
  allowToggle?: boolean;
  placeholder?: string;
  onCopied?: () => void;
}) {
  const display = value && value.trim() ? value : placeholder;
  return (
    <div className="copy-field">
      {label ? <span className="copy-field-label">{label}</span> : null}
      <div className="copy-field-value">
        <LongText text={display} lines={lines} allowToggle={allowToggle} mono={mono} />
        {value && value.trim() ? (
          <CopyButton value={value} label={`复制${label || "内容"}`} onCopied={onCopied} />
        ) : null}
      </div>
    </div>
  );
}

function isEvidenceValid(evidence?: Evidence | null): evidence is Evidence {
  return Boolean(evidence && evidence.id && evidence.sources && evidence.sources.length);
}

function mergeEvidenceCatalogs(...catalogs: Array<Evidence[] | undefined | null>) {
  const map = new Map<string, Evidence>();
  catalogs.forEach((list) => {
    (list || []).forEach((item) => {
      if (isEvidenceValid(item) && !map.has(item.id)) {
        map.set(item.id, item);
      }
    });
  });
  return Array.from(map.values());
}

function pickEvidence(catalog: Evidence[], preferredTypes: string[] = []) {
  if (!catalog.length) return null;
  for (const kind of preferredTypes) {
    const match = catalog.find((item) => item.type === kind);
    if (match) return match;
  }
  return catalog[0] || null;
}

function productStoryHasEvidence(story?: ProductStory | null): boolean {
  if (!story) return false;
  const singleClaims = [
    story.hook?.headline,
    story.hook?.subline,
    story.problem_context?.target_user,
    story.problem_context?.pain_point,
    story.problem_context?.current_bad_solution,
    story.why_it_matters_now,
    story.next_step_guidance?.if_you_are_a_builder,
    story.next_step_guidance?.if_you_are_a_pm_or_founder,
    story.next_step_guidance?.if_you_are_evaluating,
  ];
  if (singleClaims.some((item) => item && isEvidenceValid(item.evidence))) {
    return true;
  }
  const outcomes = story.what_this_repo_gives_you || [];
  if (outcomes.some((item) => item && isEvidenceValid(item.evidence))) {
    return true;
  }
  const scenarios = story.usage_scenarios || [];
  return scenarios.some((item) => item && isEvidenceValid(item.evidence));
}

function evidenceStrengthLabel(strength?: Evidence["strength"]) {
  if (strength === "strong") return "强证据";
  if (strength === "medium") return "中等证据";
  return "弱证据";
}

const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  code: "代码",
  structure: "结构",
  dependency: "依赖",
  readme: "README",
  config: "配置",
  call_graph: "调用图",
};

const EVIDENCE_RULE_LABELS: Record<string, string> = {
  readme_summary: "README 摘要",
  repo_tree_entries: "仓库结构条目",
  dependency_files: "依赖清单",
  config_files: "配置文件",
  spotlight_snippet: "代码片段",
  port_hints: "端口线索",
};

const EVIDENCE_SOURCE_KIND_LABELS: Record<string, string> = {
  readme: "README",
  structure: "结构",
  dependency: "依赖",
  config: "配置",
  file: "文件",
  section: "章节",
  symbol: "符号",
  call_graph: "调用图",
};

function evidenceTypeLabel(value?: string) {
  if (!value) return "-";
  return EVIDENCE_TYPE_LABELS[value] || value;
}

function evidenceRuleLabel(value?: string) {
  if (!value) return "-";
  return EVIDENCE_RULE_LABELS[value] || value;
}

function evidenceSourceKindLabel(value?: string) {
  if (!value) return "-";
  return EVIDENCE_SOURCE_KIND_LABELS[value] || value;
}

function formatLineRange(range?: EvidenceLineRange) {
  if (!range) return null;
  return `${range.start}-${range.end}`;
}

function EvidencePanel({
  evidence,
  evidences,
  defaultOpen,
}: {
  evidence?: Evidence | null;
  evidences?: Evidence[] | null;
  defaultOpen?: boolean;
}) {
  const list = evidence ? [evidence] : evidences || [];
  if (!list.length) return null;
  const strongest = list.reduce<Evidence["strength"]>(
    (acc, item) => {
      if (acc === "strong") return acc;
      if (item.strength === "strong") return "strong";
      if (acc === "medium" || item.strength === "medium") return "medium";
      return "weak";
    },
    "weak"
  );
  const isWeak = strongest === "weak";
  const [open, setOpen] = useState(defaultOpen ?? !isWeak);
  return (
    <div className={`evidence-panel ${isWeak ? "is-weak" : ""}`}>
      <button className="ghost evidence-toggle" type="button" onClick={() => setOpen((value) => !value)}>
        {open ? "收起证据" : "查看证据"}
      </button>
      <span className={`evidence-badge strength-${strongest}`}>{evidenceStrengthLabel(strongest)}</span>
      {isWeak ? <span className="evidence-warning">证据较弱</span> : null}
      {open ? (
        <div className="evidence-body">
          {list.map((item) => (
            <div key={item.id} className="evidence-item">
              <div className="evidence-head">
                <span className="evidence-type">{evidenceTypeLabel(item.type)}</span>
                <span className="evidence-rule">{evidenceRuleLabel(item.derivation_rule)}</span>
              </div>
              <div className="evidence-sources">
                {item.sources.map((source, index) => {
                  const lineRange = formatLineRange(source.line_range);
                  const file = source.file || source.section || source.symbol || source.kind;
                  return (
                    <div key={`${item.id}-source-${index}`} className="evidence-source">
                      <span className="evidence-source-kind">{evidenceSourceKindLabel(source.kind)}</span>
                      <span className="evidence-source-file">{file || "-"}</span>
                      {lineRange ? <span className="evidence-source-range">行 {lineRange}</span> : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function LogPanel({
  caseId,
  onCopyLogs,
  onDownloadLogs,
}: {
  caseId: string;
  onCopyLogs: () => void;
  onDownloadLogs: () => void;
}) {
  const { buildWsUrl } = useAuth();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [status, setStatus] = useState<"connecting" | "open" | "closed" | "error">(
    "connecting"
  );
  const [filters, setFilters] = useState({
    build: true,
    run: true,
    analyze: true,
    visualize: true,
    system: true,
  });
  const [keyword, setKeyword] = useState("");
  const [reconnectIn, setReconnectIn] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const reconnectAttempt = useRef(0);
  const reconnectTimer = useRef<number | null>(null);
  const connectRef = useRef<() => void>(() => {});

  const wsUrl = buildWsUrl(`/ws/logs/${caseId}`);

  const clearReconnect = () => {
    if (reconnectTimer.current) {
      window.clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    setReconnectIn(null);
  };

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimer.current) return;
    reconnectAttempt.current += 1;
    const delay = Math.min(30000, 1000 * 2 ** (reconnectAttempt.current - 1));
    setReconnectIn(delay);
    reconnectTimer.current = window.setTimeout(() => {
      reconnectTimer.current = null;
      connectRef.current();
    }, delay);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    clearReconnect();
    setStatus("connecting");
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    ws.onopen = () => {
      reconnectAttempt.current = 0;
      setStatus("open");
      setReconnectIn(null);
    };
    ws.onclose = () => {
      setStatus("closed");
      scheduleReconnect();
    };
    ws.onerror = () => {
      setStatus("error");
      scheduleReconnect();
    };
    ws.onmessage = (event) => {
      let parsed: LogEntry = {
        ts: Date.now() / 1000,
        stream: "system",
        level: "INFO",
        line: String(event.data || ""),
      };
      try {
        const data = JSON.parse(event.data as string);
        if (data && typeof data === "object") {
          parsed = {
            ts: data.ts ?? parsed.ts,
            stream: data.stream ?? parsed.stream,
            level: data.level ?? parsed.level,
            line: data.line ?? parsed.line,
          };
        }
      } catch (err) {
        parsed = {
          ...parsed,
          line: String(event.data || ""),
        };
      }
      setLogs((prev) => {
        const next = [...prev, parsed];
        if (next.length > 2000) {
          return next.slice(-2000);
        }
        return next;
      });
    };
  }, [wsUrl, scheduleReconnect]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    setLogs([]);
    connect();
    return () => {
      clearReconnect();
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect, caseId]);

  useEffect(() => {
    if (!autoScroll) return;
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "auto" });
    }
  }, [logs, autoScroll]);

  const visibleLogs = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return logs.filter((entry) => {
      const key = entry.stream as keyof typeof filters;
      if (filters[key] === false) return false;
      if (needle && !entry.line.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [logs, filters, keyword]);

  const jumpToLatest = () => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  };

  return (
    <div className="logs-panel" data-testid="logs-panel">
      <div className="logs-header">
        <div>
          <div className="section-title">日志</div>
          <div className={`ws-status ws-${status}`}>
            {status === "open" ? "已连接" : status === "connecting" ? "连接中" : "已断开"}
            {reconnectIn ? ` · ${Math.ceil(reconnectIn / 1000)} 秒后重连` : ""}
          </div>
        </div>
        <div className="logs-actions">
          <button className="ghost" type="button" onClick={onCopyLogs}>
            复制日志
          </button>
          <button className="ghost" type="button" onClick={onDownloadLogs}>
            下载 1000 行
          </button>
          <button className="ghost" type="button" onClick={() => setAutoScroll((value) => !value)}>
            {autoScroll ? "暂停滚动" : "自动滚动"}
          </button>
          <button className="ghost" type="button" onClick={jumpToLatest}>
            跳到最新
          </button>
          <button className="ghost" type="button" onClick={connect}>
            重连
          </button>
        </div>
      </div>
      <div className="filters">
        <div className="filter search">
          <input
            type="text"
            placeholder="搜索关键词"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
        </div>
        {(
          [
            { id: "build", label: "构建" },
            { id: "run", label: "运行" },
            { id: "analyze", label: "分析" },
            { id: "visualize", label: "讲解" },
            { id: "system", label: "系统" },
          ] as const
        ).map((item) => (
          <label key={item.id} className="filter">
            <input
              type="checkbox"
              checked={filters[item.id]}
              onChange={() =>
                setFilters((prev) => ({
                  ...prev,
                  [item.id]: !prev[item.id],
                }))
              }
            />
            <span>{item.label}</span>
          </label>
        ))}
      </div>
      <div className="log-list">
        {visibleLogs.length ? (
          visibleLogs.map((entry, index) => (
            <div className={`log-line level-${entry.level?.toLowerCase()}`} key={`${index}-${entry.ts}`}>
              <span className="log-ts">{typeof entry.ts === "number" ? formatTime(entry.ts) : entry.ts}</span>
              <span className={`log-stream stream-${entry.stream}`}>{streamLabel(entry.stream)}</span>
              <span className="log-msg">{entry.line}</span>
            </div>
          ))
        ) : (
          <div className="muted">暂无日志，运行后会自动出现。</div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

/* ─── Legal pages (accessible without login) ─── */

function LegalTermsPage({ navigate }: { navigate: (path: string) => void }) {
  return (
    <div className="create-grid">
      <div className="card card-hero">
        <h2 className="section-title">服务条款</h2>
        <p className="section-sub">AntiHub 平台服务条款</p>
      </div>
      <div className="card legal-content">
        <h3>1. 服务说明</h3>
        <p>AntiHub 是一个面向开发者的智能文档与项目分析平台。用户通过注册账号并选择相应的订阅套餐来使用平台提供的各项服务。</p>

        <h3>2. 账号与使用</h3>
        <p>用户须使用真实信息注册，并对账号下的所有活动承担责任。禁止将服务用于任何违反法律法规的用途。</p>

        <h3>3. 付费与订阅</h3>
        <p>平台提供多种订阅套餐，用户可通过微信扫码支付开通。具体价格与权益以购买页面展示为准。积分一经充值到账，不因套餐变更而失效。</p>

        <h3>4. 知识产权</h3>
        <p>用户上传和生成的文档内容归用户所有。平台技术、界面、品牌等知识产权归 AntiHub 团队所有。</p>

        <h3>5. 免责声明</h3>
        <p>平台按"现状"提供服务，不对因网络故障、系统维护或不可抗力导致的服务中断承担责任。平台生成的分析结果仅供参考。</p>

        <h3>6. 条款变更</h3>
        <p>AntiHub 团队保留随时修改本条款的权利，变更后将在平台公告。继续使用平台即视为同意修改后的条款。</p>

        <h3>7. 联系我们</h3>
        <p>如有疑问，请联系客服邮箱：<a href="mailto:3193773138@qq.com">3193773138@qq.com</a></p>

        <p className="muted legal-placeholder-note">本文档为首发占位版本，正式条款以后续更新为准。</p>
      </div>
      <LegalFooter navigate={navigate} />
    </div>
  );
}

function LegalPrivacyPage({ navigate }: { navigate: (path: string) => void }) {
  return (
    <div className="create-grid">
      <div className="card card-hero">
        <h2 className="section-title">隐私政策</h2>
        <p className="section-sub">AntiHub 平台隐私保护说明</p>
      </div>
      <div className="card legal-content">
        <h3>1. 信息收集</h3>
        <p>我们在您注册和使用服务时收集必要的信息，包括：用户名、邮箱地址、支付记录以及使用平台过程中产生的操作日志。</p>

        <h3>2. 信息使用</h3>
        <p>收集的信息仅用于：提供和改进服务、处理支付与订阅、发送服务相关通知、保障平台安全。我们不会将您的个人信息出售给第三方。</p>

        <h3>3. 信息存储与保护</h3>
        <p>您的数据存储于安全的服务器环境，我们采取合理的技术和管理措施保护您的信息安全。但请理解，互联网传输不能保证绝对安全。</p>

        <h3>4. 信息共享</h3>
        <p>除以下情况外，我们不会向第三方共享您的个人信息：法律法规要求、支付服务处理（微信支付）、经您明确同意。</p>

        <h3>5. 用户权利</h3>
        <p>您有权访问、更正或删除您的个人信息。如需操作，请联系客服。注销账号后，我们将在合理期限内删除您的个人数据。</p>

        <h3>6. Cookie 使用</h3>
        <p>平台使用必要的本地存储（localStorage）来维持登录状态和偏好设置。</p>

        <h3>7. 政策更新</h3>
        <p>本隐私政策可能不定期更新，更新后将在平台公告。</p>

        <h3>8. 联系方式</h3>
        <p>隐私相关问题请联系：<a href="mailto:3193773138@qq.com">3193773138@qq.com</a></p>

        <p className="muted legal-placeholder-note">本文档为首发占位版本，正式条款以后续更新为准。</p>
      </div>
      <LegalFooter navigate={navigate} />
    </div>
  );
}

function LegalRefundPage({ navigate }: { navigate: (path: string) => void }) {
  return (
    <div className="create-grid">
      <div className="card card-hero">
        <h2 className="section-title">退款政策</h2>
        <p className="section-sub">AntiHub 平台退款与售后说明</p>
      </div>
      <div className="card legal-content">
        <h3>1. 退款适用范围</h3>
        <p>以下情况可申请退款：</p>
        <ul>
          <li>支付成功但积分未到账（系统故障导致）</li>
          <li>重复支付同一订单</li>
          <li>支付后 24 小时内未使用任何积分，可申请全额退款</li>
        </ul>

        <h3>2. 不予退款的情况</h3>
        <ul>
          <li>积分已部分或全部消耗</li>
          <li>购买超过 7 天且已使用服务</li>
          <li>因用户自身原因（如违规）导致账号受限</li>
        </ul>

        <h3>3. 退款流程</h3>
        <p>请发送退款申请至客服邮箱 <a href="mailto:3193773138@qq.com">3193773138@qq.com</a>，邮件中请注明：</p>
        <ul>
          <li>注册用户名</li>
          <li>订单编号</li>
          <li>支付金额与时间</li>
          <li>退款原因</li>
        </ul>

        <h3>4. 处理时效</h3>
        <p>我们将在收到申请后 3 个工作日内审核并回复。审核通过后，退款将在 5-10 个工作日内原路返回。</p>

        <h3>5. 联系方式</h3>
        <p>退款及售后问题请联系：<a href="mailto:3193773138@qq.com">3193773138@qq.com</a></p>

        <p className="muted legal-placeholder-note">本文档为首发占位版本，正式条款以后续更新为准。</p>
      </div>
      <LegalFooter navigate={navigate} />
    </div>
  );
}

function LegalFooter({ navigate }: { navigate: (path: string) => void }) {
  return (
    <footer className="site-footer">
      <div className="site-footer-inner">
        <div className="site-footer-links">
          <button type="button" className="text-link" onClick={() => navigate("/terms")}>服务条款</button>
          <span className="site-footer-sep">|</span>
          <button type="button" className="text-link" onClick={() => navigate("/privacy")}>隐私政策</button>
          <span className="site-footer-sep">|</span>
          <button type="button" className="text-link" onClick={() => navigate("/refund")}>退款政策</button>
        </div>
        <div className="site-footer-contact">
          客服邮箱：<a href="mailto:3193773138@qq.com">3193773138@qq.com</a>
        </div>
        <div className="site-footer-copy">&copy; {new Date().getFullYear()} AntiHub 团队</div>
      </div>
    </footer>
  );
}

export default App;
