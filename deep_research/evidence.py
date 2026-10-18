"""V3 evidence ledger and checklist planning, without additional LLM calls.

Evidence for V3 comes from search-provider dictionaries, carried as opaque
receipts in ToolMessage.additional_kwargs (not from the message body or LLM).
``tool_observed`` means that the provider returned a URL and source snippet;
it is NOT a claim of URL liveness, semantic support or factual correctness.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

# Intentionally short domain templates: these are MINIMUM planning constraints,
# not an exhaustive description of the topic. Additional aspects come from the
# original brief-generation LLM response and remain model-proposed.
_DOMAIN_TEMPLATES = (
    (("投资", "investment", "investing"), ("收益与估值依据", "下行情景与主要风险")),
    (("行业", "市场前景", "industry outlook", "market outlook"),
     ("市场规模与发展趋势", "竞争格局", "成本与盈利能力", "政策与外部风险")),
)
_MAX_ASPECTS = 10
_SOURCE_PATTERN = re.compile(
    r"--- SOURCE\s+\d+:\s*(.*?)\s*---\s*\nURL:\s*(\S+)\s*\n"
    r"(?:Published:\s*(\d{4}-\d{2}-\d{2})\s*\n)?\s*SUMMARY:\s*\n"
    r"(.*?)(?=\n-{10,}\s*(?:\n|$)|\n\s*--- SOURCE\s+\d+:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PUBLISHED_PATTERN = re.compile(r"(?m)^\s*Published(?:_at| date)?:\s*(\d{4}-\d{2}-\d{2})\s*$")


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def parse_brief_plan(content: str, user_query: str) -> dict[str, Any]:
    """Build a bounded, checkpointable, *provisional* aspect checklist.

    The same brief model proposes topics, so this is not an extra LLM request.
    If the response is malformed, the checklist is NOT silently treated as
    complete. The next gate routes to HITL rather than auto-approving it.
    """
    if not isinstance(content, str):
        content = str(content)
    decoded: Any = None
    candidate = content.strip()
    try:
        decoded = json.loads(candidate)
    except (ValueError, TypeError):
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
        if fenced:
            try:
                decoded = json.loads(fenced.group(1))
            except ValueError:
                pass
    proposal: list[str] = []
    if isinstance(decoded, dict):
        brief = decoded.get("research_brief")
        brief = brief.strip() if isinstance(brief, str) else ""
        raw = decoded.get("required_aspects")
        if isinstance(raw, list):
            for item in raw[:_MAX_ASPECTS]:
                title = item.get("title") if isinstance(item, dict) else item
                if isinstance(title, str) and title.strip():
                    proposal.append(title.strip()[:120])
    else:
        brief = ""
    if not brief:
        brief = content.strip()

    # Explicit rules/templates supplement the proposal; they cannot be removed
    # by the LLM. Plan is still marked provisional until HITL confirms it.
    domain = str(user_query or "").lower()
    templated: list[str] = []
    for triggers, labels in _DOMAIN_TEMPLATES:
        if any(token in domain for token in triggers):
            templated.extend(labels)
    titles: list[str] = []
    seen: set[str] = set()
    for label in templated + proposal:
        key = " ".join(label.casefold().split())
        # Suppress near-identical dimensions such as "盈利能力" vs "成本与盈利能力".
        if key and key not in seen and not any(
            len(key) >= 3 and (key in previous or previous in key) for previous in seen
        ):
            seen.add(key)
            titles.append(label)
        if len(titles) >= _MAX_ASPECTS:
            break
    aspects = [
        {"aspect_id": f"aspect-{i:02d}", "title": title,
         "min_sources": 2, "max_age_days": None}
        for i, title in enumerate(titles, 1)
    ]
    status = "proposed" if proposal and brief else "needs_confirmation"
    return {"research_brief": brief, "required_aspects": aspects,
            "checklist_status": status,
            "checklist_fingerprint": checklist_fingerprint(aspects)}


def checklist_fingerprint(aspects: list[dict[str, Any]]) -> str:
    canonical = json.dumps(aspects, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(canonical)


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        return ""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        host = parts.hostname.lower().rstrip(".")
        if any(ch.isspace() for ch in host) or host == "localhost" or not "." in host:
            return ""
        # Strip fragments; links with different tracking parameters may still
        # remain distinct URLs, but publisher-domain counting de-duplicates them.
        port = f":{parts.port}" if parts.port else ""
        netloc = host + port
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    except (ValueError, AttributeError):
        return ""


def publisher_domain(url: str) -> str:
    """Hostname grouping proxy. NOT proof of independent ownership."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    host = urlsplit(normalized).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def collect_provider_sources(unique_results: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Create provenance receipts from the provider's structured result fields.

    Search result TEXT can contain adversarial SOURCE headers, so V3 coverage
    must not parse a ToolMessage body to invent provider-owned URL metadata.
    Summarization-model output is also excluded from the receipt's excerpt.
    """
    records: list[dict[str, Any]] = []
    if not isinstance(unique_results, Mapping):
        return records
    for raw_url, item in list(unique_results.items())[:80]:
        if not isinstance(item, Mapping):
            continue
        url = normalize_url(raw_url)
        excerpt = str(item.get("content") or item.get("raw_content") or "").strip()[:1000]
        if not url or not excerpt:
            continue
        pub = item.get("published_date") or item.get("published_at")
        published = str(pub)[:10] if isinstance(pub, str) else None
        if published:
            try:
                date.fromisoformat(published)
            except ValueError:
                published = None
        records.append({
            "source_id": "src-" + _digest(url), "source_url": url,
            "publisher_domain": publisher_domain(url),
            "title": str(item.get("title") or "")[:200],
            "claim_id": "excerpt-" + _digest(excerpt),
            "excerpt": excerpt, "excerpt_sha256": _digest(excerpt),
            "published_at": published,
            "source_status": "tool_observed",
            "relation_status": "unverified",
        })
    return records


def _is_valid_receipt(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    url, excerpt = record.get("source_url"), record.get("excerpt")
    if not isinstance(url, str) or normalize_url(url) != url:
        return False
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    return (
        record.get("source_id") == "src-" + _digest(url)
        and record.get("claim_id") == "excerpt-" + _digest(excerpt)
        and record.get("excerpt_sha256") == _digest(excerpt)
        and record.get("publisher_domain") == publisher_domain(url)
        and record.get("source_status") == "tool_observed"
        and record.get("relation_status") == "unverified"
    )

def extract_search_sources(
    messages: list[Any], *, require_receipt: bool = False,
) -> list[dict[str, Any]]:
    """Extract exact tool-returned source snippets, never model invented cites.

    Call only from the Researcher code path on *ToolMessage* instances. V3
    requires trusted structured receipts attached by the search tool wrapper;
    the older formatted-string parser is retained ONLY for non-V3 callers/tests.
    It must never supply V3 gate coverage, because source excerpts can contain
    forged search-result headers.
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for msg in messages:
        if getattr(msg, "name", None) != "tavily_search" or getattr(msg, "type", None) != "tool":
            continue
        receipt_rows = (getattr(msg, "additional_kwargs", None) or {}).get("evidence_receipts")
        if isinstance(receipt_rows, list):
            for row in receipt_rows:
                if not _is_valid_receipt(row):
                    continue
                source_id = row["source_id"]
                if source_id not in seen:
                    seen.add(source_id)
                    records.append(dict(row))
            # Never parse the body when a trusted receipt channel is available.
            continue
        if require_receipt:
            continue
        text = getattr(msg, "content", "")
        if not isinstance(text, str):
            continue
        for match in _SOURCE_PATTERN.finditer(text):
            title, raw_url, header_date, summary = match.groups()
            url = normalize_url(raw_url)
            excerpt = summary.strip()[:1000]
            if not url or not excerpt:
                continue
            source_id = "src-" + _digest(url)
            # De-dupe at extraction time; each source is independently assigned
            # to a checklist aspect by the Supervisor's explicit aspect_id.
            if source_id in seen:
                continue
            seen.add(source_id)
            published_match = _PUBLISHED_PATTERN.search(summary)
            published = header_date or (published_match.group(1) if published_match else None)
            if published:
                try:
                    date.fromisoformat(published)
                except ValueError:
                    published = None
            records.append({
                "source_id": source_id, "source_url": url,
                "publisher_domain": publisher_domain(url),
                "title": title.strip()[:200],
                "claim_id": "excerpt-" + _digest(excerpt),
                "excerpt": excerpt, "excerpt_sha256": _digest(excerpt),
                "published_at": published,
                "source_status": "tool_observed",  # NOT externally verified
                "relation_status": "unverified",   # semantic relevance NOT proved
            })
    return records[:80]


def attribute_sources(
    sources: list[dict[str, Any]], *, aspect_id: str, valid_aspects: list[dict[str, Any]],
    generation: int,
) -> list[dict[str, Any]]:
    """Only explicit delegated checklist IDs may obtain coverage credit.

    A model choosing an aspect does not prove the content is relevant; the
    Evaluator checks relevance later. Empty/invalid IDs remain unassigned.
    """
    allowed = {a.get("aspect_id") for a in valid_aspects if isinstance(a, dict)}
    assigned = aspect_id if aspect_id in allowed else None
    return [{**record, "aspect_id": assigned, "generation": generation,
             "assignment_method": "supervisor_declared" if assigned else "unassigned"}
            for record in sources if isinstance(record, dict)]


def _content_key(excerpt: str) -> str:
    """Compare conservative formatting-only rewrites; never conflate different facts."""
    import unicodedata

    normalized = unicodedata.normalize("NFKC", excerpt).casefold()
    return _digest(re.sub(r"\s+", " ", normalized).strip())


def _coverage_keys(records: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Observed NEW aspect/publisher coverage, not a claim of source independence.

    Hostnames are proxies. Repeated URLs, mirrored paths and changed snippets
    from the same domain and aspect cannot increment structural coverage.
    """
    return {
        (str(item["aspect_id"]), item["publisher_domain"])
        for item in records
        if _is_valid_receipt(item)
        and item.get("assignment_method") == "supervisor_declared"
        and isinstance(item.get("aspect_id"), str)
        and item["aspect_id"].strip()
        and type(item.get("generation")) is int
        and item["generation"] >= 1
    }


def merge_evidence(
    existing: list[dict[str, Any]], additional: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Retain novel provenance, report *effective new coverage units*.

    A second article at the same publisher can still be useful for semantic
    review and remains in the ledger if its excerpt differs. It does not count
    as an independent publisher. Formatting-only rewrites are discarded.
    The returned number is not a claim of factual correctness.
    """
    if not isinstance(existing, list) or not isinstance(additional, list):
        raise ValueError("evidence ledger must be a list")
    merged: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str]] = set()
    before_coverage: set[tuple[str, str]] = set()
    for batch_index, group in enumerate((existing, additional)):
        for item in group:
            if not isinstance(item, dict):
                raise ValueError("invalid evidence record")
            # Never let malformed candidate records inflate novelty counters.
            if not _is_valid_receipt(item):
                continue
            key = (str(item.get("aspect_id") or ""),
                   str(item.get("publisher_domain") or ""),
                   _content_key(str(item["excerpt"])))
            if key in keys:
                continue
            keys.add(key)
            merged.append(dict(item))
        if batch_index == 0:
            before_coverage = _coverage_keys(merged)
    return merged, len(_coverage_keys(merged) - before_coverage)
