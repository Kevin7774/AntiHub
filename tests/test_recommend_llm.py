from __future__ import annotations

import recommend.llm as llm


def test_extract_search_queries_prefers_technical_terms(monkeypatch) -> None:
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(
        llm,
        "_post",
        lambda _payload, timeout=25, metric_scope="recommend.llm": {
            "choices": [
                {
                    "message": {
                        "content": '["医院图片上传系统", "FileSystemWatcher", "文件增量同步", "Windows后台监控服务"]'
                    }
                }
            ]
        },
    )

    queries = llm.extract_search_queries("医院图片上传软件，核心是文件夹监控和增量同步。")
    merged = " ".join(queries)
    assert "FileSystemWatcher" in merged
    assert any("增量同步" in item for item in queries)
    assert all("医院" not in item for item in queries)


def test_extract_search_queries_fallback_when_llm_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(llm, "llm_available", lambda: False)
    try:
        llm.extract_search_queries("需要 FileSystemWatcher + Windows 后台服务 + 文件增量同步")
    except llm.RecommendLLMError as exc:
        assert "OPENAI_API_KEY" in str(exc)
        assert "MINIMAX_API_KEY" in str(exc)
    else:
        raise AssertionError("expected RecommendLLMError when llm is unavailable")


def test_extract_search_queries_parses_array_after_think_block(monkeypatch) -> None:
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(
        llm,
        "_post",
        lambda _payload, timeout=25, metric_scope="recommend.llm": {
            "choices": [
                {
                    "message": {
                        "content": (
                            "<think>分析业务背景并忽略医院语义...</think>\n"
                            "[\"FileSystemWatcher\", \"增量文件同步\", \"C# Windows后台服务\"]"
                        )
                    }
                }
            ]
        },
    )
    queries = llm.extract_search_queries("医院医生端图片上传 PRD")
    assert any("FileSystemWatcher" in item for item in queries)
    assert any("增量文件同步" in item for item in queries)
