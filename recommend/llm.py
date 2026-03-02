import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import (
    RECOMMEND_LLM_MAX_TOKENS,
    RECOMMEND_LLM_TEMPERATURE,
    build_url_opener,
)
from llm_registry import (
    anthropic_response_to_openai,
    openai_to_anthropic_payload,
    provider_available,
    resolve_provider,
)
from runtime_metrics import record_counter_metric, record_timing_metric


class RecommendLLMError(RuntimeError):
    pass


def llm_available() -> bool:
    return provider_available("recommend")


def _active_provider() -> str:
    name, *_ = resolve_provider("recommend")
    return name


def _active_api_key() -> str:
    _, api_key, *_ = resolve_provider("recommend")
    return api_key


def _active_base_url() -> str:
    _, _, base_url, *_ = resolve_provider("recommend")
    return base_url


def _active_model(default: str) -> str:
    _, _, _, model, _ = resolve_provider("recommend")
    return model or default


def _strip_think_blocks(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()


def _find_json_array_fragment(text: str) -> Optional[str]:
    raw = str(text or "")
    if not raw:
        return None
    start = -1
    depth = 0
    in_string = False
    quote_char = ""
    escaped = False
    for idx, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote_char:
                in_string = False
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote_char = ch
            continue
        if ch == "[":
            if depth == 0:
                start = idx
            depth += 1
            continue
        if ch == "]" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                return raw[start : idx + 1]
    return None


def _post(payload: Dict[str, Any], timeout: int = 25, metric_scope: str = "recommend.llm") -> Dict[str, Any]:
    name, api_key, base_url, model, api_format = resolve_provider("recommend")
    if not api_key:
        raise RecommendLLMError(
            "LLM API 密钥未配置。请设置 LLM_PROVIDER 及对应的 API Key 环境变量。"
        )

    request_payload = dict(payload or {})
    if not str(request_payload.get("model") or "").strip():
        request_payload["model"] = model or _active_model("gpt-4o-mini")

    # Build the HTTP request based on provider API format.
    if api_format == "anthropic":
        claude_payload = openai_to_anthropic_payload(request_payload)
        url = f"{base_url}/v1/messages"
        body = json.dumps(claude_payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{base_url}/chat/completions"
        body = json.dumps(request_payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = build_url_opener(url)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RecommendLLMError(f"LLM request failed: {exc.code} {detail}") from exc
    except Exception as exc:
        raise RecommendLLMError(f"LLM request failed: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RecommendLLMError(f"LLM response parse failed: {exc}") from exc

    # Normalize Claude response to OpenAI format for downstream compatibility.
    if api_format == "anthropic":
        parsed = anthropic_response_to_openai(parsed)

    if not isinstance(parsed, dict):
        raise RecommendLLMError("LLM response payload is not a JSON object")
    duration_ms = int((time.perf_counter() - started) * 1000)
    record_timing_metric(name=f"{metric_scope}.latency_ms", duration_ms=duration_ms)
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    if total_tokens > 0:
        record_counter_metric(name=f"{metric_scope}.tokens.total", value=total_tokens)
        record_counter_metric(name=f"{metric_scope}.tokens.prompt", value=prompt_tokens)
        record_counter_metric(name=f"{metric_scope}.tokens.completion", value=completion_tokens)
    return dict(parsed)


def _extract_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, dict):
                text = str(content.get("text") or "").strip()
                if text:
                    return text
            if isinstance(content, list):
                parts: List[str] = []
                for chunk in content:
                    if isinstance(chunk, str):
                        if chunk.strip():
                            parts.append(chunk)
                        continue
                    if isinstance(chunk, dict):
                        text = str(chunk.get("text") or chunk.get("content") or "").strip()
                        if text:
                            parts.append(text)
                merged = "\n".join(parts).strip()
                if merged:
                    return merged
        text = str(choices[0].get("text") or "").strip() if isinstance(choices[0], dict) else ""
        if text:
            return text
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text
    raise RecommendLLMError("LLM response missing content")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return dict(parsed)


def _extract_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    raw = _strip_think_blocks(text)
    if not raw:
        return None
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return list(parsed)
    except Exception:
        pass
    fragment = _find_json_array_fragment(raw)
    if not fragment:
        return None
    try:
        parsed = json.loads(fragment)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    return list(parsed)


_INDUSTRY_NOISE_TERMS = {
    # Medical / Education – pure industry, never useful as code search terms
    "医院",
    "医疗",
    "医生",
    "患者",
    "学校",
    "校园",
    "老师",
    "学生",
    "hospital",
    "medical",
    "doctor",
    "patient",
    "school",
    "campus",
    "teacher",
    "student",
    # Generic scene/role nouns – must be combined with tech words to be useful
    "社区",
    "居民",
    "政府",
    "企业",
    "客户",
    "物业",
    "业主",
    "住户",
    "商超",
    "government",
    "community",
    "resident",
    "enterprise",
    "customer",
}


def _clean_query_term(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[\-\d\.\)\(、\s]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n\"'`，,;；。")
    if len(text) < 2:
        return ""
    lowered = text.lower()
    # Exact match on a noise term → always filter
    if lowered in _INDUSTRY_NOISE_TERMS:
        return ""
    # If phrase contains a noise term, only keep when it also has a
    # technical / implementation marker that makes it a useful code search query.
    if any(noise in lowered for noise in _INDUSTRY_NOISE_TERMS):
        tech_markers = (
            # Chinese implementation words
            "系统", "平台", "管理", "引擎", "服务", "工具", "框架",
            "模块", "组件", "网关", "中间件", "微服务", "接口", "端",
            "SDK", "sdk", "api", "SaaS", "saas",
            "监控", "同步", "增量", "后台", "监听", "抓取", "爬虫", "队列",
            "小程序", "对账", "清分", "审计", "核销", "分账", "资金池",
            # English implementation words
            "system", "platform", "management", "engine", "service",
            "tool", "framework", "module", "gateway", "middleware",
            "watch", "sync", "filesystem", "monitor", "daemon",
            "payment", "pay", "coupon", "points", "loyalty",
            "merchant", "settlement", "audit", "app", "portal",
            "cloud", "docker", "k8s", "database", "db", "cache",
            "deploy", "container", "webhook", "callback", "signature",
            "registry", "notification", "push", "sms", "storage",
            "reconciliation", "ledger", "idempotent", "onboarding",
            "dashboard", "admin", "trail", "allocation", "pool",
        )
        if not any(marker in lowered or marker in text for marker in tech_markers):
            return ""
    return text[:80]


def extract_search_query_buckets(requirement_text: str) -> Dict[str, List[str]]:
    """Extract structured keyword buckets from requirement text.

    Returns dict with keys: implementation, repo_discovery, scenario_modules, negatives.
    Falls back to flat list wrapped as repo_discovery on parse failure.
    """
    raw_text = str(requirement_text or "").strip()
    if not raw_text:
        return {"implementation": [], "repo_discovery": [], "scenario_modules": [], "negatives": []}
    if not llm_available():
        raise RecommendLLMError(
            "深度搜索需要配置 LLM API。请设置 LLM_PROVIDER 及对应的 API Key 环境变量。"
        )

    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个资深全栈架构师兼开源检索专家。用户的输入是业务需求文档。\n"
                    "你的任务是将需求拆解为三类检索关键词 + 负面词。\n\n"
                    "## 关键词分桶规则\n"
                    "A) implementation（SDK/协议/模块名）：可直接 import 或 pip install 的技术名。\n"
                    "   中英文各至少 2 个。例：wechat-pay-v3-sdk, Redis分布式锁, coupon-engine\n"
                    "B) repo_discovery（GitHub/Gitee 双语搜索短语）：能搜到可复用仓库的短语。\n"
                    "   中英文各至少 2 个。例：wechat payment system, 积分管理系统, merchant SaaS platform\n"
                    "C) scenario_modules（业务流程/场景模块名）：用于匹配和解释，不直接搜索。\n"
                    "   例：扫码支付, 积分获取与消耗, 卡券核销, 商户入驻\n"
                    "D) negatives（排除词）：纯行业/角色名词，不能作为搜索词。\n"
                    "   例：社区, 居民, 政府, 企业, 客户, 物业\n\n"
                    "## 规则\n"
                    "1. 【严禁】在 implementation 和 repo_discovery 中出现纯行业/角色名词。\n"
                    "   必须转化为技术实现词（社区→社区管理系统, 政府补贴→补贴资金池管理系统）。\n"
                    "2. 优先提取：SDK名称、技术框架、功能模块名、系统架构组件。\n"
                    "3. implementation + repo_discovery 合计 8-12 个。\n"
                    "4. scenario_modules 3-8 个。\n\n"
                    "## 输出格式\n"
                    "纯 JSON 对象，严禁 Markdown：\n"
                    '{"implementation":["..."],"repo_discovery":["..."],'
                    '"scenario_modules":["..."],"negatives":["..."]}'
                ),
            },
            {
                "role": "user",
                "content": (
                    "请只输出 JSON 对象，不要 markdown，不要解释。\n"
                    f"需求文档如下：\n{raw_text[:4000]}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": max(256, min(RECOMMEND_LLM_MAX_TOKENS, 768)),
    }
    response = _post(payload, metric_scope="recommend.llm.query_rewrite")
    content = _extract_content(response)

    # Try structured parse first
    parsed_obj = _extract_json(content)
    if parsed_obj and isinstance(parsed_obj.get("implementation"), list):
        buckets: Dict[str, List[str]] = {"implementation": [], "repo_discovery": [], "scenario_modules": [], "negatives": []}
        for key in buckets:
            raw_items = parsed_obj.get(key) or []
            seen: set[str] = set()
            for item in raw_items:
                cleaned = _clean_query_term(item) if key != "negatives" else str(item or "").strip()
                lower = cleaned.lower()
                if not cleaned or lower in seen:
                    continue
                seen.add(lower)
                buckets[key].append(cleaned)
        return buckets

    # Fallback: try parsing as flat JSON array (backward compat with old LLM output)
    parsed_array = _extract_json_array(content)
    if parsed_array is not None:
        flat: List[str] = []
        seen_flat: set[str] = set()
        for item in parsed_array:
            cleaned = _clean_query_term(item)
            lower = cleaned.lower()
            if not cleaned or lower in seen_flat:
                continue
            seen_flat.add(lower)
            flat.append(cleaned)
        return {
            "implementation": flat[:4],
            "repo_discovery": flat[4:8],
            "scenario_modules": [],
            "negatives": [],
        }

    raise RecommendLLMError("query rewrite parse failed: expected JSON object or array")


def extract_search_queries(requirement_text: str) -> List[str]:
    """Extract flat list of search queries (backward-compatible wrapper).

    Internally uses extract_search_query_buckets and flattens
    implementation + repo_discovery into a single list.
    """
    buckets = extract_search_query_buckets(requirement_text)
    merged: List[str] = []
    seen: set[str] = set()
    for key in ("implementation", "repo_discovery"):
        for item in buckets.get(key, []):
            lower = item.lower()
            if lower not in seen:
                seen.add(lower)
                merged.append(item)
    if len(merged) < 3:
        raise RecommendLLMError(
            "深度搜索需要配置稳定可用的大模型能力。当前长文档技术词提炼失败，搜索结果可能极不准确。"
        )
    return merged[:12]


def build_requirement_profile(requirement_text: str, query: str) -> Optional[Dict[str, Any]]:
    if not llm_available():
        return None
    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": "你是技术需求分析助手，只输出 JSON，不要代码块或额外说明。",
            },
            {
                "role": "user",
                "content": (
                    "根据用户需求文本，提取搜索与匹配信息，输出 JSON：\n"
                    "{\n"
                    '  "summary": "一句话需求摘要(<=40字)",\n'
                    '  "search_query": "用于 GitHub 搜索的短语(<=12词)",\n'
                    '  "keywords": ["关键词1","关键词2"],\n'
                    '  "must_have": ["必须满足点"],\n'
                    '  "nice_to_have": ["加分项"],\n'
                    '  "target_stack": ["技术栈/语言/框架"],\n'
                    '  "scenarios": ["适用场景"]\n'
                    "}\n"
                    f"需求文本：{requirement_text}\n"
                    f"补充查询：{query}\n"
                ),
            },
        ],
        "temperature": RECOMMEND_LLM_TEMPERATURE,
        "max_tokens": RECOMMEND_LLM_MAX_TOKENS,
    }
    response = _post(payload, metric_scope="recommend.llm.profile")
    content = _extract_content(response)
    return _extract_json(content)


def decompose_requirement_modules(requirement_text: str) -> Optional[List[Dict[str, Any]]]:
    """Decompose requirement into 15-30 functional modules with reuse tags."""
    raw_text = str(requirement_text or "").strip()
    if not raw_text or not llm_available():
        return None
    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一位资深软件架构师。根据用户需求文档，将系统拆解为 15-30 个可独立实现的功能模块。\n"
                    "每个模块标注复用类别：\n"
                    '- "Reusable OSS": 可直接复用开源项目，无需或极少修改\n'
                    '- "Config-only": 仅需配置即可使用（如 Nginx/Redis/MQ）\n'
                    '- "Light customization": 需少量定制（<5人天），如改 UI 或加字段\n'
                    '- "Heavy customization": 需大量定制（>5人天），如自研核心逻辑\n\n'
                    "输出纯 JSON 数组，严禁 Markdown：\n"
                    '[{"id":"M01","name":"模块名称（含技术实现词）",'
                    '"category":"Reusable OSS|Config-only|Light customization|Heavy customization",'
                    '"actors":["参与角色"],"integrations":["外部集成点"],"compliance":["合规要求"]}]'
                ),
            },
            {
                "role": "user",
                "content": (
                    "请只输出 JSON 数组，不要 markdown，不要解释。\n"
                    f"需求文档如下：\n{raw_text[:4000]}"
                ),
            },
        ],
        "temperature": 0.3,
        "max_tokens": max(512, min(RECOMMEND_LLM_MAX_TOKENS, 1500)),
    }
    response = _post(payload, metric_scope="recommend.llm.module_decomp")
    content = _extract_content(response)
    parsed = _extract_json_array(content)
    if not parsed:
        return None
    modules: List[Dict[str, Any]] = []
    valid_categories = {"Reusable OSS", "Config-only", "Light customization", "Heavy customization"}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or f"M{len(modules) + 1:02d}").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        category = str(item.get("category") or "Light customization").strip()
        if category not in valid_categories:
            category = "Light customization"
        modules.append({
            "id": mid,
            "name": name,
            "category": category,
            "actors": [str(a) for a in (item.get("actors") or []) if str(a).strip()],
            "integrations": [str(i) for i in (item.get("integrations") or []) if str(i).strip()],
            "compliance": [str(c) for c in (item.get("compliance") or []) if str(c).strip()],
        })
    return modules if modules else None


def rank_candidates(
    requirement_summary: str,
    candidates: List[Dict[str, Any]],
    top_k: int,
) -> Optional[Dict[str, Any]]:
    if not llm_available():
        return None
    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": "你是开源仓库推荐专家，只输出 JSON，不要代码块或额外说明。",
            },
            {
                "role": "user",
                "content": (
                    "请根据需求摘要，对候选仓库进行语义匹配排序，输出 JSON：\n"
                    "{\n"
                    '  "results": [\n'
                    '    {"id": "owner/repo", "score": 0-100, '
                    '"reasons": ["匹配理由"], "tags": ["标签"], "risks": ["风险提示"]}\n'
                    "  ]\n"
                    "}\n"
                    f"需求摘要：{requirement_summary}\n"
                    f"候选仓库：{json.dumps(candidates, ensure_ascii=False)}\n"
                    f"只返回前 {top_k} 条结果，score 越高越匹配。\n"
                ),
            },
        ],
        "temperature": RECOMMEND_LLM_TEMPERATURE,
        "max_tokens": RECOMMEND_LLM_MAX_TOKENS,
    }
    response = _post(payload, metric_scope="recommend.llm.rerank")
    content = _extract_content(response)
    return _extract_json(content)


def summarize_findings(
    requirement_summary: str,
    candidates: List[Dict[str, Any]],
    max_points: int = 5,
) -> Optional[Dict[str, Any]]:
    if not llm_available():
        return None
    condensed: List[Dict[str, Any]] = []
    for item in candidates[:10]:
        condensed.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("full_name") or ""),
                "source": str(item.get("source") or ""),
                "description": str(item.get("description") or "")[:240],
                "topics": [str(topic) for topic in (item.get("topics") or [])[:6]],
                "language": str(item.get("language") or ""),
                "match_score": int(item.get("match_score") or 0),
            }
        )

    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是技术方案检索助手。你必须结合用户原始需求的技术点进行点评，"
                    "并明确指出候选项目与需求的契合关系。只输出 JSON，不要额外说明。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "根据需求与候选结果，生成检索结论。输出 JSON：\n"
                    "{\n"
                    '  "deep_summary": "一句话结论(<=120字)",\n'
                    '  "insight_points": ["要点1(需包含项目名+匹配技术点)", "要点2"]\n'
                    "}\n"
                    f"需求：{requirement_summary}\n"
                    f"候选：{json.dumps(condensed, ensure_ascii=False)}\n"
                    "请优先点评用户需求中的关键实现点（如支付、积分、卡券核销、商户管理等核心模块）"
                    "分别由哪些项目满足，并给出简短依据。\n"
                    f"insight_points 不超过 {max(2, min(max_points, 8))} 条。\n"
                ),
            },
        ],
        "temperature": RECOMMEND_LLM_TEMPERATURE,
        "max_tokens": RECOMMEND_LLM_MAX_TOKENS,
    }
    response = _post(payload, metric_scope="recommend.llm.summary")
    content = _extract_content(response)
    return _extract_json(content)
