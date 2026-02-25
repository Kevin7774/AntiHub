import json

from analyze.product_story import build_product_story
from evidence import validate_evidence


class FailingLLM:
    def generate_product_story(self, payload):
        raise RuntimeError("boom")


class SimpleLLM:
    def generate_product_story(self, payload):
        evidence_id = ""
        if payload.get("evidence"):
            evidence_id = payload["evidence"][0].get("id") or ""
        response = {
            "hook": {
                "headline": {"claim": "项目亮点", "evidence_id": evidence_id, "confidence": 0.8},
                "subline": {"claim": "适合快速了解的人", "evidence_id": evidence_id, "confidence": 0.7},
            },
            "problem_context": {
                "target_user": {"claim": "需要决策的团队", "evidence_id": evidence_id, "confidence": 0.6},
                "pain_point": {"claim": "价值难以判断", "evidence_id": evidence_id, "confidence": 0.6},
                "current_bad_solution": {"claim": "只能凭经验猜测", "evidence_id": evidence_id, "confidence": 0.5},
            },
            "what_this_repo_gives_you": [
                {"claim": "更快做出判断", "evidence_id": evidence_id, "confidence": 0.6},
                {"claim": "减少沟通成本", "evidence_id": evidence_id, "confidence": 0.6},
                {"claim": "统一价值表达", "evidence_id": evidence_id, "confidence": 0.6},
            ],
            "usage_scenarios": [
                {"claim": "评估项目适配度时", "evidence_id": evidence_id, "confidence": 0.5},
                {"claim": "需要快速对外介绍时", "evidence_id": evidence_id, "confidence": 0.5},
            ],
            "why_it_matters_now": {"claim": "节奏加快更需要清晰判断", "evidence_id": evidence_id, "confidence": 0.5},
            "next_step_guidance": {
                "if_you_are_a_builder": {"claim": "先对齐目标", "evidence_id": evidence_id, "confidence": 0.4},
                "if_you_are_a_pm_or_founder": {"claim": "确认是否匹配优先级", "evidence_id": evidence_id, "confidence": 0.4},
                "if_you_are_evaluating": {"claim": "先明确需求", "evidence_id": evidence_id, "confidence": 0.4},
            },
        }
        return json.dumps(response)


class InvalidEvidenceLLM:
    def generate_product_story(self, payload):
        return json.dumps(
            {
                "hook": {
                    "headline": {"claim": "Headline", "evidence_id": "missing", "confidence": 0.9},
                    "subline": {"claim": "Subline", "evidence_id": "missing", "confidence": 0.6},
                }
            }
        )


def test_product_story_fallback_shape() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/"]},
        "languages": [],
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
    }
    outcome = build_product_story(repo_index, "Readme excerpt", None, log=lambda _: None, llm=FailingLLM())
    story = outcome.story
    assert outcome.source == "fallback"
    assert story.get("hook", {}).get("headline")
    assert validate_evidence(story["hook"]["headline"]["evidence"])
    assert len(story.get("what_this_repo_gives_you") or []) >= 1
    assert validate_evidence(story["what_this_repo_gives_you"][0]["evidence"])
    assert story.get("why_it_matters_now")
    assert story.get("meta", {}).get("source") == "fallback"
    assert story.get("meta", {}).get("reason_code") == "UNEXPECTED_ERROR"
    assert "boom" in str(story.get("meta", {}).get("error") or "")


def test_product_story_llm_normalization() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/"]},
        "languages": [],
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
    }
    outcome = build_product_story(repo_index, "Readme excerpt", None, log=lambda _: None, llm=SimpleLLM())
    story = outcome.story
    assert outcome.source == "llm"
    assert story["hook"]["headline"]["claim"] == "项目亮点"
    assert validate_evidence(story["hook"]["headline"]["evidence"])
    assert len(story["what_this_repo_gives_you"]) == 3


def test_product_story_drops_invalid_evidence() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/"]},
        "languages": [],
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
    }
    outcome = build_product_story(repo_index, "Readme excerpt", None, log=lambda _: None, llm=InvalidEvidenceLLM())
    story = outcome.story
    assert outcome.source == "llm"
    assert story["hook"]["headline"] is None


def test_product_story_strips_html_from_readme_excerpt() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/"]},
        "languages": [],
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
    }
    readme_excerpt = '<p align="center"><img alt="Hive Banner" src="https://example.com/banner.png" /></p>'
    outcome = build_product_story(repo_index, readme_excerpt, None, log=lambda _: None, llm=FailingLLM())
    headline = (((outcome.story or {}).get("hook") or {}).get("headline") or {}).get("claim") or ""
    assert "<img" not in headline
    assert "<p " not in headline


def test_product_story_skips_language_switch_headline() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/"]},
        "languages": [],
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
    }
    readme_excerpt = "English | 简体中文 | Español | 日本語"
    outcome = build_product_story(repo_index, readme_excerpt, None, log=lambda _: None, llm=FailingLLM())
    headline = (((outcome.story or {}).get("hook") or {}).get("headline") or {}).get("claim") or ""
    assert "English" not in headline
    assert "帮助你" in headline
