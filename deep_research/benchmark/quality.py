"""E8（Phase 3C-2）：writer 输出与输入的可复算质量指标。

纯函数、无网络、无 LLM——E2E 的客观质量对比必须由确定性指标给出，
LLM judge（red_team 角色）只能作为辅助证据（见 scripts/experiments/e8_paired_judge.py）。

设计取舍：

* **claim 覆盖用 shingle 匹配**：把 claim 与报告都规范化为小写词序列，
  检查是否存在长度 ≥ ``min_shingle`` 的连续词窗口完全一致。这能抓住
  "报告原样复述了该断言"，也能抓住轻度改写前的主干；对完全改写的判断
  会偏保守（漏判为未覆盖）——宁可保守，不用 LLM 自我评价。
* **所有指标只输出计数/长度/短片段**，不落盘报告全文以外的敏感内容
  （报告本身已在 tasks.db 与本地 artifacts 中，仅本地保存）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

_WORD_RE = re.compile(r"[a-z0-9一-鿿]+")
_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.M)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.M)
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$", re.M)

_CONCLUSION_HINTS = ("conclusion", "summary", "结论", "总结", "展望", "结语")
_INTRO_HINTS = ("introduction", "overview", "background", "引言", "概述", "背景")


def normalize_text(text: str) -> str:
    """小写化 + 非字母数字折叠为单空格（中英文均可）。

    中文按**单字**切分（CJK 没有空格分词，逐字才能给出可用的 shingle 粒度）。
    """
    text = text.lower()
    text = re.sub(r"([一-鿿])", r" \1 ", text)  # 中文逐字
    words = _WORD_RE.findall(text)
    return " ".join(words)


def _words(text: str) -> list[str]:
    return normalize_text(text).split()


def text_hash(text: str) -> str:
    """安全内容哈希（sha1 前 16 位），用于跨 run 比对同一输入而不泄露原文。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ===== 报告侧指标 =====


def report_metrics(report: str | None) -> dict:
    """最终报告的结构/引用指标（全部可复算）。"""
    report = report or ""
    headings = _HEADING_RE.findall(report)
    h2_titles = [title.strip() for level, title in headings if level == "##"]
    h3_titles = [title.strip() for level, title in headings if level == "###"]
    citations = _CITATION_RE.findall(report)
    urls = _URL_RE.findall(report)
    lowered = report.lower()

    return {
        "non_empty": bool(report.strip()),
        "length_chars": len(report),
        "length_words": len(report.split()),
        "h1_count": sum(1 for level, _ in headings if level == "#"),
        "h2_count": len(h2_titles),
        "h3_count": len(h3_titles),
        "h2_titles": h2_titles,
        "citation_markers": len(citations),
        "unique_citation_indices": len(set(citations)),
        "url_count": len(urls),
        "unique_url_count": len(set(urls)),
        "list_items": len(_LIST_ITEM_RE.findall(report)),
        "table_rows": len([row for row in _TABLE_ROW_RE.findall(report) if not _TABLE_SEP_RE.match(row)]),
        "has_intro_section": any(h.lower().find(k) >= 0 for h in h2_titles for k in _INTRO_HINTS),
        "has_conclusion_section": any(h.lower().find(k) >= 0 for h in h2_titles for k in _CONCLUSION_HINTS),
        "content_hash": text_hash(report),
    }


# ===== claim 覆盖 =====
#
# 匹配规则（在真实数据上校准，2026-10-06）：
#
#   covered = 5-gram 精确 shingle 命中（原样引用）
#             或 content-word containment ≥ 0.70（去停用词的词面包含率，
#             容忍改写但要求复用 claim 的实词）
#
# 校准依据（smoke run 的 9 条 SUPPORTED claim + 5 条无关对照 claim）：
#   真实 claim 的 containment 分布 0.69–1.00（8/9 ≥ 0.69）；
#   无关对照 0.11–0.56（最高一条是与报告共享通用 ML 词汇的 transformer 断言）。
#   0.70 的阈值留出 0.14 的安全间隔。8-gram 精确匹配单独使用会漏掉 7/9
#   （writer 是改写而非复述），不能作为唯一规则。

_STOPWORDS = frozenset(
    "a an the of to in on for and or with is are was were be been by as at from that this "
    "these those it its their there which who whom whose not no into over under between "
    "during after before than then so such can could may might will would should does do "
    "did has have had".split()
)


def _shingle_set(words: list[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + size]) for i in range(max(0, len(words) - size + 1))}


def _content_words(text: str) -> list[str]:
    return [w for w in _words(text) if w not in _STOPWORDS]


def claim_coverage(
    claims: Iterable[str],
    report: str,
    *,
    min_shingle: int = 5,
    containment_threshold: float = 0.70,
) -> dict:
    """每条 claim 是否被报告覆盖；逐条返回 exact / containment 证据。"""
    report_words = _words(report)
    report_shingles = _shingle_set(report_words, min_shingle)
    report_norm = " ".join(report_words)
    report_content = set(_content_words(report))

    details, covered, uncovered = [], [], []
    for claim in claims:
        claim_words = _words(claim)
        claim_content = [w for w in claim_words if w not in _STOPWORDS]
        if not claim_words:
            continue

        if len(claim_words) < min_shingle:
            exact = " ".join(claim_words) in report_norm
        else:
            exact = bool(_shingle_set(claim_words, min_shingle) & report_shingles)
        containment = (
            sum(1 for w in claim_content if w in report_content) / len(claim_content)
            if claim_content
            else 0.0
        )
        hit = exact or containment >= containment_threshold
        details.append(
            {"claim": claim, "covered": hit, "exact": exact, "containment": round(containment, 3)}
        )
        (covered if hit else uncovered).append(claim)

    total = len(details)
    containments = sorted(d["containment"] for d in details)
    median_containment = (
        containments[len(containments) // 2]
        if len(containments) % 2
        else (containments[len(containments) // 2 - 1] + containments[len(containments) // 2]) / 2
    ) if containments else None

    return {
        "total": total,
        "covered": len(covered),
        "coverage": (len(covered) / total) if total else None,
        "exact_matches": sum(1 for d in details if d["exact"]),
        "median_containment": median_containment,
        "covered_claims": covered,
        "uncovered_claims": uncovered,
        "details": details,
    }


def verification_metrics(verdicts: list[Mapping[str, Any]] | None) -> dict:
    """把 verification details（逐 claim verdict）折算成质量信号。"""
    verdicts = list(verdicts or [])
    by_verdict: dict[str, int] = {}
    for v in verdicts:
        key = str(v.get("verdict", "UNKNOWN")).upper()
        by_verdict[key] = by_verdict.get(key, 0) + 1
    total = len(verdicts)
    unsupported = by_verdict.get("UNSUPPORTED", 0)
    return {
        "total_claims": total,
        "by_verdict": by_verdict,
        "unsupported_rate": (unsupported / total) if total else None,
        "supported_claims": [v.get("claim_text", "") for v in verdicts
                             if str(v.get("verdict", "")).upper() in ("SUPPORTED", "PARTIAL")],
        "unsupported_claims": [v.get("claim_text", "") for v in verdicts
                               if str(v.get("verdict", "")).upper() == "UNSUPPORTED"],
    }


# ===== writer 输入侧（§12）=====


def writer_input_metrics(
    *,
    research_brief: str = "",
    draft_report: str = "",
    notes: list[str] | None = None,
    warning: str = "",
    source_count: int | None = None,
    prompt: str | None = None,
) -> dict:
    """writer 输入工作量摘要：只含长度/计数/哈希，不含原文。"""
    notes = list(notes or [])
    metrics = {
        "research_brief_chars": len(research_brief or ""),
        "draft_report_chars": len(draft_report or ""),
        "notes_count": len(notes),
        "notes_chars": sum(len(n) for n in notes),
        "warning_chars": len(warning or ""),
        "source_count": source_count,
    }
    if prompt is not None:
        metrics["prompt_chars"] = len(prompt)
        metrics["prompt_hash"] = text_hash(prompt)
    return metrics


# ===== A/B 统计 =====


def summarize(values: list[float | int | None]) -> dict:
    """min / median / max（+ mean 补充）；None 值与空集处理为 None。"""
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return {"n": 0, "min": None, "median": None, "max": None, "mean": None}
    n = len(clean)
    median = clean[n // 2] if n % 2 else (clean[n // 2 - 1] + clean[n // 2]) / 2
    return {
        "n": n,
        "min": clean[0],
        "median": median,
        "max": clean[-1],
        "mean": sum(clean) / n,
    }


def delta(on_summary: Mapping[str, Any], off_summary: Mapping[str, Any]) -> dict:
    """OFF − ON：absolute 与 percentage 都必须报告（median 为主）。"""

    def _d(key: str) -> dict:
        a, b = on_summary.get(key), off_summary.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return {"on": a, "off": b, "absolute": None, "percent": None}
        absolute = b - a
        percent = (absolute / a * 100) if a else None
        return {"on": a, "off": b, "absolute": absolute, "percent": percent}

    return {key: _d(key) for key in ("min", "median", "max", "mean")}
