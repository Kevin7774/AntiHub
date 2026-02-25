export type KnowledgeNodeType =
  | "module"
  | "service"
  | "data"
  | "document"
  | "process"
  | "concept";

export type KnowledgeNode = {
  id: string;
  label: string;
  type: KnowledgeNodeType;
  x: number;
  y: number;
  note?: string;
  score?: number;
};

export type KnowledgeEdge = {
  id: string;
  source: string;
  target: string;
  relation: string;
  weight: number;
};

export type KnowledgeGraph = {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  meta: {
    source: string;
    generated_at: string;
    sentence_count: number;
    source_length: number;
  };
};

export type KnowledgeGraphAnalysis = {
  summary?: string;
  key_findings?: string[];
  risk_signals?: string[];
  suggestions?: string[];
  focus_modules?: string[];
};

const RELATION_KEYWORDS: Array<{ relation: string; terms: RegExp[] }> = [
  { relation: "调用", terms: [/调用/i, /\bcall/i, /\binvoke/i] },
  { relation: "依赖", terms: [/依赖/i, /\bdepend/i, /\brequire/i] },
  { relation: "使用", terms: [/使用/i, /\buse/i, /\butilize/i] },
  { relation: "包含", terms: [/包含/i, /\bcontain/i, /\binclude/i, /\bconsist/i] },
  { relation: "提供", terms: [/提供/i, /\bprovide/i, /\bexpose/i, /\bserve/i] },
  { relation: "存储", terms: [/存储/i, /\bstore/i, /\bpersist/i, /\bsave/i] },
  { relation: "处理", terms: [/处理/i, /\bprocess/i, /\bhandle/i] },
  { relation: "连接", terms: [/连接/i, /\bconnect/i, /\blink/i, /\bbridge/i] },
];

const COMMON_STOPWORDS = new Set([
  "以及",
  "其中",
  "如果",
  "因为",
  "所以",
  "然后",
  "这个",
  "那个",
  "我们",
  "你们",
  "他们",
  "当前",
  "支持",
  "可以",
  "主要",
  "用于",
  "通过",
  "进行",
  "实现",
  "相关",
  "功能",
  "系统",
  "模块",
  "服务",
  "组件",
  "说明",
  "文档",
  "项目",
  "仓库",
  "代码",
  "流程",
  "以及",
  "the",
  "and",
  "for",
  "with",
  "that",
  "from",
  "this",
  "using",
  "into",
  "your",
  "their",
]);

const ENTITY_HINTS: Array<{ type: KnowledgeNodeType; patterns: RegExp[] }> = [
  { type: "service", patterns: [/service$/i, /服务$/, /gateway$/i, /client$/i] },
  { type: "module", patterns: [/module$/i, /controller$/i, /handler$/i, /router$/i, /模块$/] },
  { type: "data", patterns: [/repo/i, /repository/i, /store/i, /db$/i, /database/i, /表$/, /数据/] },
  { type: "document", patterns: [/readme/i, /文档/, /manual/i, /guide/i] },
  { type: "process", patterns: [/pipeline/i, /workflow/i, /任务/, /流程/] },
];

function stripCodeBlocks(markdown: string) {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`\n]+)`/g, " $1 ")
    .replace(/!\[[^\]]*]\([^)]+\)/g, " ")
    .replace(/\[[^\]]+]\([^)]+\)/g, " ");
}

function normalizeEntity(value: string) {
  return value
    .replace(/^[\s\-_*`"'“”‘’.,:;()【】[\]{}<>]+|[\s\-_*`"'“”‘’.,:;()【】[\]{}<>]+$/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function normalizedKey(value: string) {
  return normalizeEntity(value).toLowerCase();
}

function isLikelyEntity(value: string) {
  const candidate = normalizeEntity(value);
  if (!candidate) return false;
  if (candidate.length < 2) return false;
  if (/^\d+$/.test(candidate)) return false;
  if (COMMON_STOPWORDS.has(candidate.toLowerCase())) return false;
  if (/^[\u4e00-\u9fa5]{2,}$/.test(candidate)) {
    if (COMMON_STOPWORDS.has(candidate)) return false;
  }
  return true;
}

function inferNodeType(label: string): KnowledgeNodeType {
  for (const item of ENTITY_HINTS) {
    if (item.patterns.some((pattern) => pattern.test(label))) {
      return item.type;
    }
  }
  return "concept";
}

function inferRelation(sentence: string) {
  for (const item of RELATION_KEYWORDS) {
    if (item.terms.some((pattern) => pattern.test(sentence))) {
      return item.relation;
    }
  }
  return "关联";
}

function splitSentences(text: string) {
  return text
    .replace(/\r/g, "\n")
    .split(/[\n。！？!?；;]+/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

function collectCandidates(markdown: string) {
  const text = stripCodeBlocks(markdown);
  const score = new Map<string, { label: string; value: number }>();
  const add = (raw: string, weight: number) => {
    const normalized = normalizeEntity(raw);
    if (!isLikelyEntity(normalized)) return;
    const key = normalizedKey(normalized);
    const prev = score.get(key);
    if (prev) {
      prev.value += weight;
      if (normalized.length > prev.label.length) {
        prev.label = normalized;
      }
    } else {
      score.set(key, { label: normalized, value: weight });
    }
  };

  const headingMatches = text.matchAll(/^\s{0,3}#{1,6}\s+(.+)$/gm);
  for (const match of headingMatches) {
    add(match[1], 2.8);
  }

  const backtickMatches = markdown.matchAll(/`([^`\n]{2,80})`/g);
  for (const match of backtickMatches) {
    add(match[1], 2.6);
  }

  const camelMatches = text.matchAll(/\b(?:[A-Z][A-Za-z0-9_/-]{1,48}|[a-z]+(?:[A-Z][A-Za-z0-9]+)+)\b/g);
  for (const match of camelMatches) {
    add(match[0], 2.2);
  }

  const chineseMatches = text.matchAll(/[\u4e00-\u9fa5]{2,12}/g);
  for (const match of chineseMatches) {
    add(match[0], 1.4);
  }

  const quotedMatches = text.matchAll(/[“"]([^"”]{2,40})[”"]/g);
  for (const match of quotedMatches) {
    add(match[1], 1.8);
  }

  const tokens = text.matchAll(/\b[a-z][a-z0-9_/-]{2,32}\b/gi);
  for (const match of tokens) {
    const token = String(match[0] || "");
    if (token.length <= 3) continue;
    if (/^[a-z]+$/.test(token) && COMMON_STOPWORDS.has(token.toLowerCase())) continue;
    add(token, 1);
  }

  return Array.from(score.values())
    .sort((a, b) => (b.value === a.value ? b.label.length - a.label.length : b.value - a.value))
    .map((item) => item.label);
}

function buildLayout(nodeIds: string[], degreeMap: Map<string, number>) {
  const positions = new Map<string, { x: number; y: number }>();
  const sorted = [...nodeIds].sort((a, b) => (degreeMap.get(b) || 0) - (degreeMap.get(a) || 0));
  const layerSize = 10;
  sorted.forEach((id, index) => {
    const layer = Math.floor(index / layerSize);
    const slot = index % layerSize;
    const angle = ((Math.PI * 2) / layerSize) * slot + layer * 0.24;
    const base = 150 + layer * 86 + (degreeMap.get(id) || 0) * 6;
    const x = Math.cos(angle) * base;
    const y = Math.sin(angle) * base;
    positions.set(id, { x, y });
  });
  return positions;
}

function safeNodeId(label: string, usedIds: Set<string>) {
  const base = label
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
    .replace(/^-+|-+$/g, "");
  const pref = base || "node";
  if (!usedIds.has(pref)) {
    usedIds.add(pref);
    return pref;
  }
  let count = 2;
  while (usedIds.has(`${pref}-${count}`)) {
    count += 1;
  }
  const next = `${pref}-${count}`;
  usedIds.add(next);
  return next;
}

export function createEmptyKnowledgeGraph(source = "empty"): KnowledgeGraph {
  return {
    nodes: [],
    edges: [],
    meta: {
      source,
      generated_at: new Date().toISOString(),
      sentence_count: 0,
      source_length: 0,
    },
  };
}

export function buildKnowledgeGraphFromMarkdown(
  markdown: string,
  options?: { source?: string; maxNodes?: number }
): KnowledgeGraph {
  const sourceText = markdown || "";
  const sentences = splitSentences(stripCodeBlocks(sourceText));
  const candidates = collectCandidates(sourceText);
  const maxNodes = Math.max(10, Math.min(60, options?.maxNodes ?? 36));
  const chosenLabels = candidates.slice(0, maxNodes);

  const nodeIdByLabel = new Map<string, string>();
  const usedIds = new Set<string>();
  const nodes: KnowledgeNode[] = chosenLabels.map((label, index) => {
    const id = safeNodeId(label, usedIds);
    nodeIdByLabel.set(label, id);
    return {
      id,
      label,
      type: inferNodeType(label),
      x: 0,
      y: 0,
      score: maxNodes - index,
    };
  });

  const edgeCount = new Map<string, { source: string; target: string; relation: string; weight: number }>();
  const candidateList = nodes.map((node) => node.label);
  const byLength = [...candidateList].sort((a, b) => b.length - a.length);

  sentences.forEach((sentence) => {
    if (!sentence || sentence.length < 4) return;
    const mentions = byLength
      .map((label) => {
        const index = sentence.indexOf(label);
        if (index < 0) return null;
        return { label, index };
      })
      .filter((item): item is { label: string; index: number } => Boolean(item))
      .sort((a, b) => a.index - b.index);

    if (mentions.length < 2) return;
    const relation = inferRelation(sentence);
    for (let i = 0; i < mentions.length - 1; i += 1) {
      const left = mentions[i];
      const right = mentions[i + 1];
      if (left.label === right.label) continue;
      const source = nodeIdByLabel.get(left.label);
      const target = nodeIdByLabel.get(right.label);
      if (!source || !target || source === target) continue;
      const key = `${source}__${target}__${relation}`;
      const prev = edgeCount.get(key);
      if (prev) {
        prev.weight += 1;
      } else {
        edgeCount.set(key, { source, target, relation, weight: 1 });
      }
    }
  });

  if (edgeCount.size === 0 && nodes.length > 1) {
    for (let i = 0; i < nodes.length - 1; i += 1) {
      const source = nodes[i];
      const target = nodes[i + 1];
      edgeCount.set(`${source.id}__${target.id}__关联`, {
        source: source.id,
        target: target.id,
        relation: "关联",
        weight: 1,
      });
    }
  }

  const edges: KnowledgeEdge[] = Array.from(edgeCount.values()).map((item, index) => ({
    id: `edge-${index + 1}`,
    source: item.source,
    target: item.target,
    relation: item.relation,
    weight: item.weight,
  }));

  const degreeMap = new Map<string, number>();
  nodes.forEach((node) => degreeMap.set(node.id, 0));
  edges.forEach((edge) => {
    degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + edge.weight);
    degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + edge.weight);
  });
  const layout = buildLayout(
    nodes.map((node) => node.id),
    degreeMap
  );

  const laidOutNodes = nodes.map((node) => {
    const point = layout.get(node.id) || { x: 0, y: 0 };
    return {
      ...node,
      x: point.x,
      y: point.y,
    };
  });

  return {
    nodes: laidOutNodes,
    edges,
    meta: {
      source: options?.source || "manual_markdown",
      generated_at: new Date().toISOString(),
      sentence_count: sentences.length,
      source_length: sourceText.length,
    },
  };
}

function asNodeType(raw: unknown): KnowledgeNodeType {
  if (raw === "module" || raw === "service" || raw === "data" || raw === "document" || raw === "process") {
    return raw;
  }
  return "concept";
}

export function parseKnowledgeGraphJson(raw: unknown): KnowledgeGraph | null {
  if (!raw || typeof raw !== "object") return null;
  const input = raw as Partial<KnowledgeGraph>;
  if (!Array.isArray(input.nodes) || !Array.isArray(input.edges)) return null;

  const nodeIds = new Set<string>();
  const nodes: KnowledgeNode[] = [];
  input.nodes.forEach((node, index) => {
    if (!node || typeof node !== "object") return;
    const typed = node as Partial<KnowledgeNode>;
    const label = normalizeEntity(String(typed.label || ""));
    if (!label) return;
    let id = normalizeEntity(String(typed.id || ""));
    if (!id) {
      id = `node-${index + 1}`;
    }
    if (nodeIds.has(id)) return;
    nodeIds.add(id);
    nodes.push({
      id,
      label,
      type: asNodeType(typed.type),
      x: Number.isFinite(Number(typed.x)) ? Number(typed.x) : 0,
      y: Number.isFinite(Number(typed.y)) ? Number(typed.y) : 0,
      note: typed.note ? String(typed.note) : undefined,
      score: Number.isFinite(Number(typed.score)) ? Number(typed.score) : undefined,
    });
  });

  const edges: KnowledgeEdge[] = [];
  const edgeIds = new Set<string>();
  input.edges.forEach((edge, index) => {
    if (!edge || typeof edge !== "object") return;
    const typed = edge as Partial<KnowledgeEdge>;
    const source = normalizeEntity(String(typed.source || ""));
    const target = normalizeEntity(String(typed.target || ""));
    if (!source || !target || source === target) return;
    if (!nodeIds.has(source) || !nodeIds.has(target)) return;
    let id = normalizeEntity(String(typed.id || ""));
    if (!id) id = `edge-${index + 1}`;
    if (edgeIds.has(id)) return;
    edgeIds.add(id);
    edges.push({
      id,
      source,
      target,
      relation: normalizeEntity(String(typed.relation || "关联")) || "关联",
      weight: Number.isFinite(Number(typed.weight)) ? Math.max(1, Number(typed.weight)) : 1,
    });
  });

  return {
    nodes,
    edges,
    meta: {
      source: normalizeEntity(String(input.meta?.source || "imported")) || "imported",
      generated_at: String(input.meta?.generated_at || new Date().toISOString()),
      sentence_count: Number(input.meta?.sentence_count || 0),
      source_length: Number(input.meta?.source_length || 0),
    },
  };
}

export function parseKnowledgeGraphAsset(raw: unknown): { graph: KnowledgeGraph | null; analysis: KnowledgeGraphAnalysis | null } {
  if (!raw || typeof raw !== "object") {
    return { graph: null, analysis: null };
  }
  const payload = raw as { analysis?: unknown };
  const graph = parseKnowledgeGraphJson(raw);
  let analysis: KnowledgeGraphAnalysis | null = null;
  if (payload.analysis && typeof payload.analysis === "object") {
    const typed = payload.analysis as Record<string, unknown>;
    analysis = {
      summary: typed.summary ? String(typed.summary) : undefined,
      key_findings: Array.isArray(typed.key_findings) ? typed.key_findings.map(String) : undefined,
      risk_signals: Array.isArray(typed.risk_signals) ? typed.risk_signals.map(String) : undefined,
      suggestions: Array.isArray(typed.suggestions) ? typed.suggestions.map(String) : undefined,
      focus_modules: Array.isArray(typed.focus_modules) ? typed.focus_modules.map(String) : undefined,
    };
  }
  return { graph, analysis };
}
