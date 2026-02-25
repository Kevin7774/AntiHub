import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List

from config import (
    ANALYZE_LLM_MAX_TOKENS,
    ANALYZE_LLM_TEMPERATURE,
    OPENAI_API_KEY,
    OPENAI_API_MODEL,
    OPENAI_BASE_URL,
)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    markdown: str
    mermaids: List[str]


class LLMAdapter:
    def generate(self, signals: Dict[str, Any]) -> str:
        raise NotImplementedError

    def generate_business_summary(self, payload: Dict[str, Any]) -> str:
        raise NotImplementedError

    def generate_product_story(self, payload: Dict[str, Any]) -> str:
        raise NotImplementedError

    def translate_markdown(self, markdown: str) -> str:
        raise NotImplementedError

    def repair_mermaid(self, mermaid_code: str, error_message: str) -> str:
        raise NotImplementedError


class OpenAIChatAdapter(LLMAdapter):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = ANALYZE_LLM_TEMPERATURE,
        max_tokens: int = ANALYZE_LLM_MAX_TOKENS,
        timeout: int = 30,
    ) -> None:
        self._api_key = api_key
        self._base_url = self._normalize_base_url(base_url)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            return "https://api.openai.com/v1"
        if base.endswith("/v1"):
            return base
        return f"{base}/v1"

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise LLMError(f"LLM request failed: {exc.code} {detail}") from exc
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        try:
            return json.loads(data)
        except Exception as exc:
            raise LLMError(f"LLM response parse failed: {exc}") from exc

    def _extract_content(self, payload: Dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("LLM response missing choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise LLMError("LLM response missing content")
        return str(content)

    def generate(self, signals: Dict[str, Any]) -> str:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        system_prompt = (
            "You are a senior technical writer. Generate a concise product explain doc in Simplified Chinese. "
            "Return ONLY valid JSON with keys: markdown, mermaids."
        )
        user_prompt = (
            "Use the provided signals to write a Markdown doc with sections: "
            "功能概览, 快速开始, 架构概览, 依赖与限制, 常见问题, 与平台一键体验的对应入口. "
            "Do NOT include any env values; only list env keys if needed. "
            "All narrative text must be in Simplified Chinese. "
            "Do not translate code blocks, commands, file paths, or identifiers. "
            "Also return at least one Mermaid diagram in mermaids list. "
            f"Signals JSON: {json.dumps(signals, ensure_ascii=False)}"
        )
        payload = {
            "model": self._model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        response = self._post("/chat/completions", payload)
        return self._extract_content(response)

    def generate_business_summary(self, payload: Dict[str, Any]) -> str:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        system_prompt = (
            "You are a product marketer writing for non-technical business stakeholders. "
            "Return ONLY valid JSON with keys: "
            "slogan, business_values, business_scenarios, readme_marketing_cards. "
            "Avoid technical jargon, code terms, file paths, or line numbers. "
            "Use Simplified Chinese for all output text."
        )
        user_prompt = (
            "Create a business-oriented, non-technical summary that answers: "
            "'Why should I care about this project?'\n"
            "Rules:\n"
            "- slogan: 1 concise line.\n"
            "- business_values: exactly 3 bullets.\n"
            "- business_scenarios: 2 bullets.\n"
            "- readme_marketing_cards: 2-5 items, each with title and description.\n"
            "- If README goals/features are provided, rewrite them in customer-friendly language.\n"
            "- If they are missing, infer safely and mark tone as cautious.\n"
            "- Output all claims in Simplified Chinese.\n"
            f"Input JSON: {json.dumps(payload, ensure_ascii=False)}"
        )
        request = {
            "model": self._model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": min(0.4, max(0.1, self._temperature)),
            "max_tokens": min(900, self._max_tokens),
        }
        response = self._post("/chat/completions", request)
        return self._extract_content(response)

    def generate_product_story(self, payload: Dict[str, Any]) -> str:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        system_prompt = (
            "You are a product storyteller writing for non-technical business stakeholders. "
            "Return ONLY valid JSON with the exact keys and nested structure requested. "
            "Avoid technical jargon, code terms, file paths, or architecture details. "
            "Use Simplified Chinese for all output text."
        )
        user_prompt = (
            "Create a sales-grade, non-technical product narrative optimized for a first-time reader "
            "to decide within 3 minutes if this project is worth attention.\n"
            "STRICT OUTPUT CONTRACT:\n"
            "{\n"
            "  \"hook\": {\n"
            "    \"headline\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    \"subline\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0}\n"
            "  },\n"
            "  \"problem_context\": {\n"
            "    \"target_user\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    \"pain_point\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    \"current_bad_solution\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0}\n"
            "  },\n"
            "  \"what_this_repo_gives_you\": [\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0}\n"
            "  ],\n"
            "  \"usage_scenarios\": [\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0}\n"
            "  ],\n"
            "  \"why_it_matters_now\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "  \"next_step_guidance\": {\n"
            "    \"if_you_are_a_builder\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    \"if_you_are_a_pm_or_founder\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0},\n"
            "    \"if_you_are_evaluating\": {\"claim\": \"...\", \"evidence_id\": \"...\", \"confidence\": 0.0}\n"
            "  }\n"
            "}\n"
            "Rules:\n"
            "- No code snippets, file names, or architecture talk.\n"
            "- Plain language, short sentences.\n"
            "- Do not assume it runs successfully or is production-ready.\n"
            "- Do not require Docker, deployment, or execution.\n"
            "- Prefer clarity over completeness.\n"
            "- You MUST select evidence_id from the provided evidence list; do not invent ids.\n"
            "- Output all claims in Simplified Chinese.\n"
            f"Input JSON: {json.dumps(payload, ensure_ascii=False)}"
        )
        request = {
            "model": self._model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": min(0.5, max(0.2, self._temperature)),
            "max_tokens": min(1200, self._max_tokens),
        }
        response = self._post("/chat/completions", request)
        return self._extract_content(response)

    def translate_markdown(self, markdown: str) -> str:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        system_prompt = (
            "You are a technical writer translating Markdown into Simplified Chinese. "
            "Preserve structure and formatting. "
            "Do NOT translate text inside code fences, inline code, URLs, file paths, or identifiers. "
            "Return ONLY the translated Markdown."
        )
        user_prompt = (
            "Translate the following Markdown into Simplified Chinese. "
            "Keep code blocks and inline code unchanged.\n\n"
            f"{markdown}"
        )
        payload = {
            "model": self._model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": min(1600, self._max_tokens),
        }
        response = self._post("/chat/completions", payload)
        return self._extract_content(response)

    def repair_mermaid(self, mermaid_code: str, error_message: str) -> str:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        system_prompt = (
            "You fix Mermaid diagrams. Return ONLY the corrected Mermaid code, no fences."
        )
        user_prompt = (
            "Fix the Mermaid diagram below so it renders. "
            "Keep it minimal and valid.\n\n"
            f"Error: {error_message}\n\n"
            f"Mermaid:\n{mermaid_code}"
        )
        payload = {
            "model": self._model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": min(800, self._max_tokens),
        }
        response = self._post("/chat/completions", payload)
        return self._extract_content(response)


def get_default_llm() -> LLMAdapter:
    api_key = OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")
    base_url = (
        os.getenv("OPENAI_API_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or OPENAI_BASE_URL
        or ""
    )
    model = os.getenv("OPENAI_API_MODEL") or os.getenv("OPENAI_MODEL") or OPENAI_API_MODEL or ""
    return OpenAIChatAdapter(api_key=api_key, base_url=base_url, model=model)
