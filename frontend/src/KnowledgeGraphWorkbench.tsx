import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildKnowledgeGraphFromMarkdown,
  createEmptyKnowledgeGraph,
  parseKnowledgeGraphJson,
  type KnowledgeGraphAnalysis,
  type KnowledgeEdge,
  type KnowledgeGraph,
  type KnowledgeNode,
  type KnowledgeNodeType,
} from "./knowledge-graph";
import { joinUrl } from "./utils";

type ToastLike = {
  type: "success" | "error";
  message: string;
  detail?: string;
};

type ManualResponse = {
  case_id: string;
  manual_markdown: string;
};

type KnowledgeGraphWorkbenchProps = {
  caseId: string;
  apiBase: string;
  seedText?: string | null;
  initialGraph?: KnowledgeGraph | null;
  analysis?: KnowledgeGraphAnalysis | null;
  pushToast?: (toast: ToastLike) => void;
  onPublishGraph?: (graph: KnowledgeGraph) => void;
};

type DragState =
  | {
      kind: "pan";
      startClientX: number;
      startClientY: number;
      startX: number;
      startY: number;
    }
  | {
      kind: "node";
      nodeId: string;
      startClientX: number;
      startClientY: number;
      startX: number;
      startY: number;
    }
  | null;

const NODE_TYPE_OPTIONS: Array<{ value: KnowledgeNodeType; label: string }> = [
  { value: "concept", label: "概念" },
  { value: "module", label: "模块" },
  { value: "service", label: "服务" },
  { value: "data", label: "数据" },
  { value: "document", label: "文档" },
  { value: "process", label: "流程" },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function nodeTypeLabel(type: KnowledgeNodeType) {
  return NODE_TYPE_OPTIONS.find((item) => item.value === type)?.label || "概念";
}

function nodeColor(type: KnowledgeNodeType) {
  if (type === "service") return "#264b6f";
  if (type === "module") return "#4f396f";
  if (type === "data") return "#1f5d4d";
  if (type === "document") return "#7a5130";
  if (type === "process") return "#355a7d";
  return "#4d5562";
}

function randomNodePosition(index: number) {
  const ring = Math.floor(index / 8) + 1;
  const angle = ((Math.PI * 2) / 8) * (index % 8) + ring * 0.22;
  const radius = 120 + ring * 78;
  return {
    x: Math.cos(angle) * radius,
    y: Math.sin(angle) * radius,
  };
}

function edgeLabelPosition(source: KnowledgeNode, target: KnowledgeNode) {
  return {
    x: (source.x + target.x) / 2,
    y: (source.y + target.y) / 2,
  };
}

function safeNodeId(label: string, nodes: KnowledgeNode[]) {
  const base = label
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
    .replace(/^-+|-+$/g, "");
  const seed = base || "entity";
  const used = new Set(nodes.map((item) => item.id));
  if (!used.has(seed)) return seed;
  let count = 2;
  while (used.has(`${seed}-${count}`)) {
    count += 1;
  }
  return `${seed}-${count}`;
}

function safeEdgeId(edges: KnowledgeEdge[]) {
  const used = new Set(edges.map((item) => item.id));
  let count = edges.length + 1;
  while (used.has(`edge-${count}`)) {
    count += 1;
  }
  return `edge-${count}`;
}

export default function KnowledgeGraphWorkbench({
  caseId,
  apiBase,
  seedText,
  initialGraph,
  analysis,
  pushToast,
  onPublishGraph,
}: KnowledgeGraphWorkbenchProps) {
  const storageKey = `antihub:knowledge-graph:${caseId}`;
  const [sourceText, setSourceText] = useState(seedText || "");
  const [graph, setGraph] = useState<KnowledgeGraph>(() =>
    initialGraph?.nodes?.length || initialGraph?.edges?.length
      ? initialGraph
      : seedText?.trim()
      ? buildKnowledgeGraphFromMarkdown(seedText, { source: "seed_text" })
      : createEmptyKnowledgeGraph("empty")
  );
  const [docLoading, setDocLoading] = useState(false);
  const [docError, setDocError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [dragState, setDragState] = useState<DragState>(null);
  const [viewport, setViewport] = useState({ x: 0, y: 0, scale: 1 });
  const [canvasSize, setCanvasSize] = useState({ width: 960, height: 560 });
  const [newNodeLabel, setNewNodeLabel] = useState("");
  const [newNodeType, setNewNodeType] = useState<KnowledgeNodeType>("concept");
  const [newEdgeSource, setNewEdgeSource] = useState("");
  const [newEdgeTarget, setNewEdgeTarget] = useState("");
  const [newEdgeRelation, setNewEdgeRelation] = useState("关联");
  const [sourceExpanded, setSourceExpanded] = useState(false);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const selectedNode = useMemo(
    () => graph.nodes.find((node) => node.id === selectedNodeId) || null,
    [graph.nodes, selectedNodeId]
  );
  const selectedEdge = useMemo(
    () => graph.edges.find((edge) => edge.id === selectedEdgeId) || null,
    [graph.edges, selectedEdgeId]
  );
  const nodeById = useMemo(() => {
    const map = new Map<string, KnowledgeNode>();
    graph.nodes.forEach((item) => map.set(item.id, item));
    return map;
  }, [graph.nodes]);
  const relationStats = useMemo(() => {
    const map = new Map<string, number>();
    graph.edges.forEach((edge) => {
      map.set(edge.relation, (map.get(edge.relation) || 0) + 1);
    });
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1]);
  }, [graph.edges]);

  const buildFromText = useCallback(
    (text: string, source: string) => {
      if (!text.trim()) {
        const empty = createEmptyKnowledgeGraph(source);
        setGraph(empty);
        setSelectedNodeId(null);
        setSelectedEdgeId(null);
        return empty;
      }
      const next = buildKnowledgeGraphFromMarkdown(text, {
        source,
        maxNodes: 42,
      });
      setGraph(next);
      setSelectedNodeId(null);
      setSelectedEdgeId(null);
      setViewport({ x: 0, y: 0, scale: 1 });
      if (next.nodes[0]) {
        setNewEdgeSource(next.nodes[0].id);
      }
      if (next.nodes[1]) {
        setNewEdgeTarget(next.nodes[1].id);
      }
      return next;
    },
    [setGraph]
  );

  const loadManual = useCallback(
    async (silent = false) => {
      setDocLoading(true);
      setDocError(null);
      try {
        const response = await fetch(joinUrl(apiBase, `/cases/${caseId}/manual`));
        if (!response.ok) {
          throw new Error(`manual_not_ready_${response.status}`);
        }
        const data = (await response.json()) as ManualResponse;
        const manualText = data.manual_markdown || "";
        setSourceText(manualText);
        if (!silent) {
          pushToast?.({
            type: "success",
            message: "说明书已加载",
            detail: `字符数: ${manualText.length}`,
          });
        }
        return manualText;
      } catch (error) {
        const fallback = seedText?.trim() || "";
        if (fallback) {
          setSourceText(fallback);
          if (!silent) {
            pushToast?.({
              type: "error",
              message: "说明书暂不可用，已回退到摘要文本",
            });
          }
          return fallback;
        }
        setDocError("当前案例暂未生成说明书，请稍后重试。");
        if (!silent) {
          pushToast?.({
            type: "error",
            message: "无法读取说明书",
            detail: String(error),
          });
        }
        return "";
      } finally {
        setDocLoading(false);
      }
    },
    [apiBase, caseId, pushToast, seedText]
  );

  useEffect(() => {
    const saved = window.localStorage.getItem(storageKey);
    if (saved) {
      try {
        const parsed = parseKnowledgeGraphJson(JSON.parse(saved));
        if (parsed) {
          const shouldUseSaved =
            parsed.nodes.length > 0 ||
            parsed.edges.length > 0 ||
            !initialGraph ||
            (!initialGraph.nodes.length && !initialGraph.edges.length);
          if (shouldUseSaved) {
            setGraph(parsed);
            setSelectedNodeId(null);
            setSelectedEdgeId(null);
            setSourceText(seedText || "");
            return;
          }
        }
      } catch (error) {
        window.localStorage.removeItem(storageKey);
      }
    }
    if (initialGraph?.nodes?.length || initialGraph?.edges?.length) {
      setSourceText(seedText || "");
      setGraph(initialGraph);
    } else if (seedText?.trim()) {
      setSourceText(seedText);
      setGraph(buildKnowledgeGraphFromMarkdown(seedText, { source: "seed_text", maxNodes: 42 }));
    } else {
      setSourceText("");
      setGraph(createEmptyKnowledgeGraph("empty"));
    }
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  }, [storageKey, seedText, initialGraph]);

  useEffect(() => {
    if (seedText?.trim()) return;
    if (graph.nodes.length || graph.edges.length) return;
    void loadManual(true).then((text) => {
      if (text.trim()) {
        buildFromText(text, "manual_markdown");
      }
    });
  }, [seedText, graph.nodes.length, graph.edges.length, loadManual, buildFromText]);

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(graph));
  }, [graph, storageKey]);

  useEffect(() => {
    if (!shellRef.current) return;
    const updateSize = () => {
      if (!shellRef.current) return;
      const rect = shellRef.current.getBoundingClientRect();
      setCanvasSize({
        width: Math.max(320, rect.width),
        height: Math.max(360, rect.height),
      });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(shellRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const stopDrag = () => setDragState(null);
    window.addEventListener("mouseup", stopDrag);
    return () => window.removeEventListener("mouseup", stopDrag);
  }, []);

  const handleCanvasWheel = (event: React.WheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.08 : 0.92;
    setViewport((prev) => ({
      ...prev,
      scale: clamp(prev.scale * factor, 0.35, 2.8),
    }));
  };

  const handleCanvasMouseMove = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!dragState) return;
    if (dragState.kind === "pan") {
      const dx = event.clientX - dragState.startClientX;
      const dy = event.clientY - dragState.startClientY;
      setViewport((prev) => ({
        ...prev,
        x: dragState.startX + dx,
        y: dragState.startY + dy,
      }));
      return;
    }
    if (dragState.kind === "node") {
      const dx = (event.clientX - dragState.startClientX) / viewport.scale;
      const dy = (event.clientY - dragState.startClientY) / viewport.scale;
      setGraph((prev) => ({
        ...prev,
        nodes: prev.nodes.map((node) =>
          node.id === dragState.nodeId
            ? {
                ...node,
                x: dragState.startX + dx,
                y: dragState.startY + dy,
              }
            : node
        ),
      }));
    }
  };

  const handleCanvasMouseDown = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    setDragState({
      kind: "pan",
      startClientX: event.clientX,
      startClientY: event.clientY,
      startX: viewport.x,
      startY: viewport.y,
    });
  };

  const handleAutoBuild = async () => {
    let text = sourceText.trim();
    if (!text) {
      text = (await loadManual())?.trim() || "";
    }
    if (!text) {
      setDocError("缺少可用于自动构图的文档内容。");
      return;
    }
    const next = buildFromText(text, "manual_markdown");
    pushToast?.({
      type: "success",
      message: "知识图谱已自动构建",
      detail: `节点 ${next.nodes.length} / 关系 ${next.edges.length}`,
    });
    onPublishGraph?.(next);
  };

  const handleLoadBackendGraph = () => {
    if (!initialGraph || (!initialGraph.nodes.length && !initialGraph.edges.length)) {
      pushToast?.({
        type: "error",
        message: "后端图谱暂不可用",
      });
      return;
    }
    setGraph(initialGraph);
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    setViewport({ x: 0, y: 0, scale: 1 });
    pushToast?.({
      type: "success",
      message: "已加载后端图谱",
      detail: `节点 ${initialGraph.nodes.length} / 关系 ${initialGraph.edges.length}`,
    });
    onPublishGraph?.(initialGraph);
  };

  const handlePublishGraph = () => {
    onPublishGraph?.(graph);
    pushToast?.({
      type: "success",
      message: "已同步到结构关系图",
      detail: `节点 ${graph.nodes.length} / 关系 ${graph.edges.length}`,
    });
  };

  const handleAddNode = () => {
    const label = newNodeLabel.trim();
    if (!label) return;
    setGraph((prev) => {
      const id = safeNodeId(label, prev.nodes);
      const position = randomNodePosition(prev.nodes.length);
      const nextNode: KnowledgeNode = {
        id,
        label,
        type: newNodeType,
        x: position.x,
        y: position.y,
      };
      return {
        ...prev,
        nodes: [...prev.nodes, nextNode],
      };
    });
    setNewNodeLabel("");
  };

  const handleAddEdge = () => {
    if (!newEdgeSource || !newEdgeTarget || newEdgeSource === newEdgeTarget) return;
    const relation = newEdgeRelation.trim() || "关联";
    setGraph((prev) => {
      const exists = prev.edges.find(
        (item) =>
          item.source === newEdgeSource &&
          item.target === newEdgeTarget &&
          item.relation.toLowerCase() === relation.toLowerCase()
      );
      if (exists) {
        return {
          ...prev,
          edges: prev.edges.map((item) =>
            item.id === exists.id
              ? {
                  ...item,
                  weight: item.weight + 1,
                }
              : item
          ),
        };
      }
      const next: KnowledgeEdge = {
        id: safeEdgeId(prev.edges),
        source: newEdgeSource,
        target: newEdgeTarget,
        relation,
        weight: 1,
      };
      return {
        ...prev,
        edges: [...prev.edges, next],
      };
    });
  };

  const handleDeleteSelected = () => {
    if (selectedNodeId) {
      setGraph((prev) => ({
        ...prev,
        nodes: prev.nodes.filter((node) => node.id !== selectedNodeId),
        edges: prev.edges.filter((edge) => edge.source !== selectedNodeId && edge.target !== selectedNodeId),
      }));
      setSelectedNodeId(null);
      setSelectedEdgeId(null);
      return;
    }
    if (selectedEdgeId) {
      setGraph((prev) => ({
        ...prev,
        edges: prev.edges.filter((edge) => edge.id !== selectedEdgeId),
      }));
      setSelectedEdgeId(null);
    }
  };

  const handleExport = () => {
    const blob = new Blob([JSON.stringify(graph, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `knowledge-graph-${caseId}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  };

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = parseKnowledgeGraphJson(JSON.parse(text));
      if (!parsed) {
        throw new Error("invalid_graph");
      }
      setGraph(parsed);
      setSelectedNodeId(null);
      setSelectedEdgeId(null);
      pushToast?.({
        type: "success",
        message: "图谱导入成功",
        detail: `节点 ${parsed.nodes.length} / 关系 ${parsed.edges.length}`,
      });
    } catch (error) {
      pushToast?.({
        type: "error",
        message: "图谱导入失败",
        detail: "请确认 JSON 文件格式正确。",
      });
    } finally {
      event.target.value = "";
    }
  };

  const handleClearGraph = () => {
    setGraph(createEmptyKnowledgeGraph("manual_clear"));
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  };

  const selectedSummary = selectedNode
    ? `已选节点: ${selectedNode.label}`
    : selectedEdge
      ? `已选关系: ${selectedEdge.relation}`
      : "点击节点或关系查看详情";

  return (
    <section className="card knowledge-lab" data-testid="knowledge-lab">
      <div className="knowledge-lab-head">
        <div>
          <div className="section-title">知识图谱工作台</div>
          <div className="section-sub">自动从文档抽取实体关系，并支持手动编辑和导入导出。</div>
        </div>
        <div className="knowledge-lab-status">
          <span className={`knowledge-status-dot ${docLoading ? "loading" : "online"}`} />
          <span>{docLoading ? "文档加载中" : "可编辑"}</span>
        </div>
      </div>

      <div className="knowledge-lab-layout">
        <div className="knowledge-canvas-card">
          <div className="knowledge-toolbar">
            <div className="knowledge-toolbar-left">
              <button className="ghost" type="button" onClick={handleAutoBuild} disabled={docLoading}>
                自动构图
              </button>
              <button className="ghost" type="button" onClick={() => void loadManual()} disabled={docLoading}>
                读取说明书
              </button>
              <button className="ghost" type="button" onClick={handleLoadBackendGraph}>
                读取后端图谱
              </button>
              <button className="ghost" type="button" onClick={() => setSourceExpanded((value) => !value)}>
                {sourceExpanded ? "收起文档" : "查看文档源"}
              </button>
              <button className="ghost" type="button" onClick={handlePublishGraph}>
                同步到结构图
              </button>
            </div>
            <div className="knowledge-toolbar-right">
              <button className="ghost" type="button" onClick={() => setViewport((v) => ({ ...v, scale: clamp(v.scale + 0.2, 0.35, 2.8) }))}>
                放大
              </button>
              <button className="ghost" type="button" onClick={() => setViewport((v) => ({ ...v, scale: clamp(v.scale - 0.2, 0.35, 2.8) }))}>
                缩小
              </button>
              <button className="ghost" type="button" onClick={() => setViewport({ x: 0, y: 0, scale: 1 })}>
                重置视角
              </button>
              <button className="ghost" type="button" onClick={handleExport}>
                导出
              </button>
              <button className="ghost" type="button" onClick={() => fileInputRef.current?.click()}>
                导入
              </button>
              <button className="ghost" type="button" onClick={handleClearGraph}>
                清空
              </button>
              <input ref={fileInputRef} type="file" accept="application/json" hidden onChange={handleImport} />
            </div>
          </div>

          {sourceExpanded ? (
            <div className="knowledge-source-editor">
              <textarea
                value={sourceText}
                onChange={(event) => setSourceText(event.target.value)}
                placeholder="可粘贴说明书/README 内容，点击“自动构图”生成实体关系网络。"
                rows={8}
              />
            </div>
          ) : null}

          {docError ? <div className="error-banner">{docError}</div> : null}

          <div
            className="knowledge-canvas-shell"
            ref={shellRef}
            onMouseDown={handleCanvasMouseDown}
            onMouseMove={handleCanvasMouseMove}
            onMouseUp={() => setDragState(null)}
            onMouseLeave={() => setDragState(null)}
          >
            <svg className="knowledge-canvas-svg" width={canvasSize.width} height={canvasSize.height} onWheel={handleCanvasWheel}>
              <defs>
                <marker
                  id={`kg-arrow-${caseId}`}
                  viewBox="0 0 10 10"
                  refX="8"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(26, 38, 59, 0.6)" />
                </marker>
              </defs>
              <g transform={`translate(${canvasSize.width / 2 + viewport.x} ${canvasSize.height / 2 + viewport.y}) scale(${viewport.scale})`}>
                {graph.edges.map((edge) => {
                  const source = nodeById.get(edge.source);
                  const target = nodeById.get(edge.target);
                  if (!source || !target) return null;
                  const labelPoint = edgeLabelPosition(source, target);
                  const selected = selectedEdgeId === edge.id;
                  return (
                    <g key={edge.id}>
                      <line
                        x1={source.x}
                        y1={source.y}
                        x2={target.x}
                        y2={target.y}
                        className={`kg-edge${selected ? " is-selected" : ""}`}
                        markerEnd={`url(#kg-arrow-${caseId})`}
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelectedEdgeId(edge.id);
                          setSelectedNodeId(null);
                        }}
                      />
                      <text
                        x={labelPoint.x}
                        y={labelPoint.y - 6}
                        className={`kg-edge-label${selected ? " is-selected" : ""}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelectedEdgeId(edge.id);
                          setSelectedNodeId(null);
                        }}
                      >
                        {edge.relation}
                      </text>
                    </g>
                  );
                })}
                {graph.nodes.map((node) => {
                  const radius = clamp(13 + (node.score || 0) * 0.2, 13, 24);
                  const selected = selectedNodeId === node.id;
                  return (
                    <g
                      key={node.id}
                      transform={`translate(${node.x} ${node.y})`}
                      onMouseDown={(event) => {
                        event.stopPropagation();
                        setSelectedNodeId(node.id);
                        setSelectedEdgeId(null);
                        setDragState({
                          kind: "node",
                          nodeId: node.id,
                          startClientX: event.clientX,
                          startClientY: event.clientY,
                          startX: node.x,
                          startY: node.y,
                        });
                      }}
                    >
                      <circle
                        r={radius}
                        className={`kg-node${selected ? " is-selected" : ""}`}
                        fill={nodeColor(node.type)}
                      />
                      <text className="kg-node-label" x={radius + 8} y={4}>
                        {node.label}
                      </text>
                    </g>
                  );
                })}
              </g>
            </svg>

            <div className="knowledge-overlay-panel">
              <div className="knowledge-overlay-title">图谱统计</div>
              <div className="knowledge-overlay-grid">
                <span>节点</span>
                <strong>{graph.nodes.length}</strong>
                <span>关系</span>
                <strong>{graph.edges.length}</strong>
                <span>缩放</span>
                <strong>{Math.round(viewport.scale * 100)}%</strong>
              </div>
              <div className="knowledge-overlay-sub">{selectedSummary}</div>
            </div>
          </div>
        </div>

        <aside className="knowledge-side">
          <div className="knowledge-side-card">
            <div className="knowledge-side-title">新增节点</div>
            <div className="knowledge-form">
              <input
                type="text"
                placeholder="节点名称，例如：AuthService"
                value={newNodeLabel}
                onChange={(event) => setNewNodeLabel(event.target.value)}
              />
              <select value={newNodeType} onChange={(event) => setNewNodeType(event.target.value as KnowledgeNodeType)}>
                {NODE_TYPE_OPTIONS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <button className="primary" type="button" onClick={handleAddNode}>
                添加节点
              </button>
            </div>
          </div>

          <div className="knowledge-side-card">
            <div className="knowledge-side-title">新增关系</div>
            <div className="knowledge-form">
              <select value={newEdgeSource} onChange={(event) => setNewEdgeSource(event.target.value)}>
                <option value="">选择起点</option>
                {graph.nodes.map((node) => (
                  <option key={`edge-source-${node.id}`} value={node.id}>
                    {node.label}
                  </option>
                ))}
              </select>
              <select value={newEdgeTarget} onChange={(event) => setNewEdgeTarget(event.target.value)}>
                <option value="">选择终点</option>
                {graph.nodes.map((node) => (
                  <option key={`edge-target-${node.id}`} value={node.id}>
                    {node.label}
                  </option>
                ))}
              </select>
              <input
                type="text"
                placeholder="关系，例如：调用"
                value={newEdgeRelation}
                onChange={(event) => setNewEdgeRelation(event.target.value)}
              />
              <button className="primary" type="button" onClick={handleAddEdge}>
                添加关系
              </button>
            </div>
          </div>

          <div className="knowledge-side-card">
            <div className="knowledge-side-title">选中编辑</div>
            {selectedNode ? (
              <div className="knowledge-form">
                <input
                  type="text"
                  value={selectedNode.label}
                  onChange={(event) => {
                    const next = event.target.value;
                    setGraph((prev) => ({
                      ...prev,
                      nodes: prev.nodes.map((node) =>
                        node.id === selectedNode.id
                          ? {
                              ...node,
                              label: next,
                            }
                          : node
                      ),
                    }));
                  }}
                />
                <select
                  value={selectedNode.type}
                  onChange={(event) => {
                    const nextType = event.target.value as KnowledgeNodeType;
                    setGraph((prev) => ({
                      ...prev,
                      nodes: prev.nodes.map((node) =>
                        node.id === selectedNode.id
                          ? {
                              ...node,
                              type: nextType,
                            }
                          : node
                      ),
                    }));
                  }}
                >
                  {NODE_TYPE_OPTIONS.map((item) => (
                    <option key={`edit-node-${item.value}`} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
                <textarea
                  rows={3}
                  placeholder="备注（可选）"
                  value={selectedNode.note || ""}
                  onChange={(event) => {
                    const note = event.target.value;
                    setGraph((prev) => ({
                      ...prev,
                      nodes: prev.nodes.map((node) =>
                        node.id === selectedNode.id
                          ? {
                              ...node,
                              note,
                            }
                          : node
                      ),
                    }));
                  }}
                />
                <div className="knowledge-selected-meta">
                  <span>类型: {nodeTypeLabel(selectedNode.type)}</span>
                  <span>ID: {selectedNode.id}</span>
                </div>
              </div>
            ) : null}
            {selectedEdge ? (
              <div className="knowledge-form">
                <input
                  type="text"
                  value={selectedEdge.relation}
                  onChange={(event) => {
                    const relation = event.target.value;
                    setGraph((prev) => ({
                      ...prev,
                      edges: prev.edges.map((edge) =>
                        edge.id === selectedEdge.id
                          ? {
                              ...edge,
                              relation,
                            }
                          : edge
                      ),
                    }));
                  }}
                />
                <label className="knowledge-range">
                  <span>权重</span>
                  <input
                    type="range"
                    min={1}
                    max={10}
                    value={selectedEdge.weight}
                    onChange={(event) => {
                      const weight = Number(event.target.value);
                      setGraph((prev) => ({
                        ...prev,
                        edges: prev.edges.map((edge) =>
                          edge.id === selectedEdge.id
                            ? {
                                ...edge,
                                weight,
                              }
                            : edge
                        ),
                      }));
                    }}
                  />
                </label>
                <div className="knowledge-selected-meta">
                  <span>
                    {nodeById.get(selectedEdge.source)?.label || selectedEdge.source} {"->"}{" "}
                    {nodeById.get(selectedEdge.target)?.label || selectedEdge.target}
                  </span>
                </div>
              </div>
            ) : null}
            {!selectedNode && !selectedEdge ? <div className="muted">选择画布中的节点或关系后可编辑。</div> : null}
            <button className="ghost danger" type="button" onClick={handleDeleteSelected} disabled={!selectedNode && !selectedEdge}>
              删除选中项
            </button>
          </div>

          <div className="knowledge-side-card">
            <div className="knowledge-side-title">逻辑分析</div>
            {analysis?.summary ? <div className="knowledge-analysis-summary">{analysis.summary}</div> : null}
            {analysis?.key_findings?.length ? (
              <div className="knowledge-analysis-group">
                <div className="knowledge-analysis-label">关键发现</div>
                <ul className="knowledge-analysis-list">
                  {analysis.key_findings.map((item, index) => (
                    <li key={`finding-${index}`}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {analysis?.risk_signals?.length ? (
              <div className="knowledge-analysis-group">
                <div className="knowledge-analysis-label">风险提示</div>
                <ul className="knowledge-analysis-list">
                  {analysis.risk_signals.map((item, index) => (
                    <li key={`risk-${index}`}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {analysis?.suggestions?.length ? (
              <div className="knowledge-analysis-group">
                <div className="knowledge-analysis-label">建议动作</div>
                <ul className="knowledge-analysis-list">
                  {analysis.suggestions.map((item, index) => (
                    <li key={`suggestion-${index}`}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {!analysis?.summary && !analysis?.key_findings?.length ? (
              <div className="muted">暂无后端逻辑分析，可先自动构图再手动补充。</div>
            ) : null}
          </div>

          <div className="knowledge-side-card">
            <div className="knowledge-side-title">关系分布</div>
            {relationStats.length ? (
              <div className="knowledge-relations">
                {relationStats.map(([relation, count]) => (
                  <div key={relation} className="knowledge-rel-item">
                    <span>{relation}</span>
                    <strong>{count}</strong>
                  </div>
                ))}
              </div>
            ) : (
              <div className="muted">暂无关系数据。</div>
            )}
          </div>
        </aside>
      </div>
    </section>
  );
}
