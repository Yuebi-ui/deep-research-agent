"""测试搜索工具 —— 去重和格式化纯函数。"""

from deep_research.tools.tool import deduplicate_search_results, format_search_output


def test_deduplicate_removes_duplicate_urls(duplicate_search_results):
    """同一 URL 出现两次应只保留一次。"""
    result = deduplicate_search_results(duplicate_search_results)
    assert len(result) == 1
    assert "https://example.com/a" in result


def test_deduplicate_keeps_unique_urls(sample_search_results):
    """不同 URL 应全部保留。"""
    result = deduplicate_search_results(sample_search_results)
    assert len(result) == 2
    assert "https://example.com/a" in result
    assert "https://example.com/b" in result


def test_format_search_output_with_results(sample_search_results):
    """格式化输出应包含 SOURCE 标记和 URL。"""
    unique = deduplicate_search_results(sample_search_results)
    # 跳过摘要步骤（需要调 LLM），直接测试格式化
    summarized = {}
    for url, r in unique.items():
        summarized[url] = {"title": r["title"], "content": r["content"]}
    output = format_search_output(summarized)
    assert "SOURCE 1" in output
    assert "https://example.com/a" in output
    assert "文章 A" in output


def test_format_search_output_empty():
    """无结果时应返回提示信息。"""
    output = format_search_output({})
    assert "No valid search results found" in output
