"""Final Report 引用关系的零模型成本静态校验（告警，不篡改最终报告）。"""

from __future__ import annotations

import re

_SOURCE_HEADING = re.compile(r"(?m)^#{2,3}\s+(?:来源列表|参考文献)\s*$")
_SOURCE_LINE = re.compile(r"^\s*\[(\d+)\]\s+(.+)$")
_CITATION = re.compile(r"(?<!!)\[(\d+)\](?!\()")
_URL = re.compile(r"https?://[^\s\]\)>，,]+")


def validate_report_citations(report: str, source_material: str) -> dict:
    """检查编号是否有定义、连续、URL 是否来源于研究材料。

    不宣称证明了某个 Claim 得到证实；这只是必要非充分的引用完整性检查。
    """
    headings = list(_SOURCE_HEADING.finditer(report))
    if not headings:
        body, sources = report, ""
    else:
        split = headings[-1]
        body, sources = report[:split.start()], report[split.end():]

    used = {int(num) for num in _CITATION.findall(body)}
    defined: dict[int, str] = {}
    duplicates: set[int] = set()
    invalid_urls: set[int] = set()
    ungrounded: set[int] = set()
    for line in sources.splitlines():
        match = _SOURCE_LINE.match(line)
        if not match:
            continue
        index = int(match.group(1))
        urls = _URL.findall(match.group(2))
        if index in defined:
            duplicates.add(index)
        if not urls:
            invalid_urls.add(index)
        else:
            url = urls[0].rstrip(".。;")
            if url not in source_material:
                ungrounded.add(index)
            defined[index] = url

    expected = set(range(1, max(defined, default=0) + 1))
    issues: list[str] = []
    if used and not headings:
        issues.append("missing_source_section")
    if used - defined.keys():
        issues.append("unresolved_citations")
    if expected - defined.keys():
        issues.append("noncontiguous_source_numbers")
    if duplicates:
        issues.append("duplicate_source_numbers")
    if invalid_urls:
        issues.append("missing_source_url")
    if ungrounded:
        issues.append("url_not_found_in_research_material")
    if defined.keys() - used:
        issues.append("unused_sources")
    if not defined and _URL.search(source_material):
        issues.append("no_citations_with_available_sources")

    return {
        "ok": not issues, "issues": issues,
        "body_citations": sorted(used), "source_numbers": sorted(defined),
        "ungrounded_source_numbers": sorted(ungrounded),
    }
