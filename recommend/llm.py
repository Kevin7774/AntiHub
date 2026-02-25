import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import (
    MINIMAX_API_KEY,
    MINIMAX_BASE_URL,
    MINIMAX_MODEL,
    OPENAI_API_KEY,
    OPENAI_API_MODEL,
    OPENAI_BASE_URL,
    RECOMMEND_LLM_MAX_TOKENS,
    RECOMMEND_LLM_TEMPERATURE,
)
from runtime_metrics import record_counter_metric, record_timing_metric


class RecommendLLMError(RuntimeError):
    pass


def _normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1"
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def llm_available() -> bool:
    return bool(MINIMAX_API_KEY or OPENAI_API_KEY)


def _active_provider() -> str:
    if MINIMAX_API_KEY:
        return "minimax"
    if OPENAI_API_KEY:
        return "openai"
    return "none"


def _active_api_key() -> str:
    if MINIMAX_API_KEY:
        return str(MINIMAX_API_KEY).strip()
    return str(OPENAI_API_KEY).strip()


def _active_base_url() -> str:
    if MINIMAX_API_KEY:
        return _normalize_base_url(MINIMAX_BASE_URL or "https://api.minimax.chat/v1")
    return _normalize_base_url(OPENAI_BASE_URL or "https://api.openai.com/v1")


def _active_model(default: str) -> str:
    if MINIMAX_API_KEY:
        return str(MINIMAX_MODEL or "MiniMax-M2.5").strip() or "MiniMax-M2.5"
    return str(OPENAI_API_MODEL or default).strip() or default


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
    api_key = _active_api_key()
    if not api_key:
        raise RecommendLLMError("OPENAI_API_KEY 或 MINIMAX_API_KEY 缺失")
    base_url = _active_base_url()
    url = f"{base_url}/chat/completions"
    request_payload = dict(payload or {})
    if not str(request_payload.get("model") or "").strip():
        request_payload["model"] = _active_model("gpt-4o-mini")
    body = json.dumps(request_payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # nosec B310
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
    "医院",
    "医疗",
    "医生",
    "患者",
    "学校",
    "校园",
    "老师",
    "学生",
    "社区",
    "居民",
    "政府",
    "企业",
    "客户",
    "hospital",
    "medical",
    "doctor",
    "patient",
    "school",
    "campus",
    "teacher",
    "student",
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
    if lowered in _INDUSTRY_NOISE_TERMS:
        return ""
    if any(noise in lowered for noise in _INDUSTRY_NOISE_TERMS):
        tech_markers = (
            "watch",
            "sync",
            "service",
            "filesystem",
            "monitor",
            "daemon",
            "监控",
            "同步",
            "增量",
            "后台",
            "服务",
            "监听",
            "抓取",
            "爬虫",
            "队列",
        )
        if not any(marker in lowered or marker in text for marker in tech_markers):
            return ""
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", text) and text in _INDUSTRY_NOISE_TERMS:
        return ""
    return text[:80]


def extract_search_queries(requirement_text: str) -> List[str]:
    raw_text = str(requirement_text or "").strip()
    if not raw_text:
        return []
    if not llm_available():
        raise RecommendLLMError(
            "深度搜索需要配置 MINIMAX_API_KEY（或 OPENAI_API_KEY）。当前长文档无法提炼技术关键词，搜索结果可能极不准确。"
        )

    payload = {
        "model": _active_model("gpt-4o-mini"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个资深全栈架构师。用户的输入是业务 PRD。"
                    "请忽略行业背景，提取核心技术实现点。提取 3-5 个技术查询词，"
                    "如 ['FileSystemWatcher', '增量文件同步', 'AES加密上传', 'Windows后台服务']。"
                    "返回格式必须是纯 JSON 字符串数组，严禁包含任何 Markdown 格式。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请只输出 JSON 数组，不要 markdown，不要解释，不要对象。\n"
                    f"需求文档如下：\n{raw_text[:4000]}"
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": max(256, min(RECOMMEND_LLM_MAX_TOKENS, 512)),
    }
    response = _post(payload, metric_scope="recommend.llm.query_rewrite")
    content = _extract_content(response)
    parsed = _extract_json_array(content)
    if parsed is None:
        raise RecommendLLMError("query rewrite parse failed: expected JSON array")

    normalized_queries: List[str] = []
    seen: set[str] = set()
    for item in parsed:
        query = _clean_query_term(item)
        key = query.lower()
        if not query or key in seen:
            continue
        seen.add(key)
        normalized_queries.append(query)
    if len(normalized_queries) < 3:
        raise RecommendLLMError(
            "深度搜索需要配置稳定可用的大模型能力。当前长文档技术词提炼失败，搜索结果可能极不准确。"
        )
    return normalized_queries[:5]


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
                    "请优先点评用户需求中的关键实现点（如文件监控、增量同步、Windows 后台服务）"
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
