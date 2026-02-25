import { describe, expect, it } from "vitest";
import { buildKnowledgeGraphFromMarkdown, parseKnowledgeGraphAsset, parseKnowledgeGraphJson } from "../knowledge-graph";

describe("knowledge-graph", () => {
  it("builds graph from markdown entities and relations", () => {
    const markdown = [
      "# 架构说明",
      "AuthService 调用 UserRepository 并使用 PostgreSQL。",
      "`OrderService` 依赖 `PaymentGateway`。",
      "PaymentGateway 连接 StripeAPI。",
    ].join("\n");
    const graph = buildKnowledgeGraphFromMarkdown(markdown, { source: "test_markdown", maxNodes: 24 });
    expect(graph.nodes.length).toBeGreaterThan(3);
    expect(graph.edges.length).toBeGreaterThan(1);
    const labels = graph.nodes.map((node) => node.label.toLowerCase());
    expect(labels.some((label) => label.includes("authservice"))).toBe(true);
    expect(labels.some((label) => label.includes("userrepository"))).toBe(true);
    expect(graph.meta.source).toBe("test_markdown");
  });

  it("returns empty graph for empty text", () => {
    const graph = buildKnowledgeGraphFromMarkdown("", { source: "empty_case" });
    expect(graph.nodes).toHaveLength(0);
    expect(graph.edges).toHaveLength(0);
    expect(graph.meta.source).toBe("empty_case");
  });

  it("validates imported graph json", () => {
    const parsed = parseKnowledgeGraphJson({
      nodes: [
        { id: "auth-service", label: "AuthService", type: "service", x: 12, y: 18 },
        { id: "user-repo", label: "UserRepository", type: "data", x: -20, y: 6 },
      ],
      edges: [
        { id: "edge-1", source: "auth-service", target: "user-repo", relation: "调用", weight: 2 },
      ],
      meta: {
        source: "import",
        generated_at: "2026-02-10T00:00:00.000Z",
        sentence_count: 4,
        source_length: 200,
      },
    });
    expect(parsed).not.toBeNull();
    expect(parsed?.nodes).toHaveLength(2);
    expect(parsed?.edges).toHaveLength(1);
    expect(parsed?.nodes[0].type).toBe("service");
  });

  it("rejects invalid graph json", () => {
    expect(parseKnowledgeGraphJson({})).toBeNull();
    expect(
      parseKnowledgeGraphJson({
        nodes: [{ id: "a", label: "A" }],
        edges: [{ id: "e1", source: "a", target: "missing", relation: "关联" }],
      })
    ).not.toBeNull();
    expect(
      parseKnowledgeGraphJson({
        nodes: [{ id: "a", label: "A" }],
        edges: [{ id: "e2", source: "a", target: "a", relation: "关联" }],
      })?.edges
    ).toHaveLength(0);
  });

  it("parses backend knowledge graph asset analysis", () => {
    const result = parseKnowledgeGraphAsset({
      nodes: [{ id: "repo", label: "demo", type: "concept", x: 0, y: 0 }],
      edges: [],
      meta: { source: "backend", generated_at: "2026-02-10T00:00:00.000Z", sentence_count: 1, source_length: 12 },
      analysis: {
        summary: "图谱构建完成",
        key_findings: ["识别 1 个实体"],
      },
    });
    expect(result.graph).not.toBeNull();
    expect(result.analysis?.summary).toBe("图谱构建完成");
    expect(result.analysis?.key_findings?.[0]).toContain("实体");
  });
});
