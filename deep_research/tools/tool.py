#***********************************************
#      Filename: tools.py
#   Description: 可调用工具列表  
#***********************************************

import hashlib
import time
from typing import Optional
from urllib.parse import urlparse
from typing_extensions import Annotated, List, Literal
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool, InjectedToolArg

from deep_research import logging as dr_logging
from deep_research.utils import get_today_str, parse_json_response
from deep_research.llm import get_chat_model
from deep_research.states import Summary
from deep_research.prompts import SUMMARIZE_PROMPT, REFINE_DRAFT_REPORT_PROMPT
from deep_research.callbacks.baseline_metrics import get_baseline_collector
from deep_research.tools.search_factory import (
    SearchConfigError,
    get_search_client,
    get_search_defaults,
    get_search_provider,
)


logger = dr_logging.get_logger(__name__)

summarization_model = get_chat_model("researcher_summarizer")
writer_model = get_chat_model("writer")
MAX_CONTEXT_LENGTH = 250000
DEFAULT_MAX_CONTEXT = 1000
search_provider = None
search_client = None
search_defaults = None


def reset_search_runtime() -> None:
    """清空搜索运行时的惰性单例，供测试隔离使用。"""
    global search_provider, search_client, search_defaults
    search_provider = None
    search_client = None
    search_defaults = None


def _ensure_search_runtime(raise_on_error: bool = True):
    """初始化搜索提供client/默认值以处理自定义搜索后端。
    """

    global search_provider, search_client, search_defaults

    try:
        if search_provider is None:
            logger.debug("Resolving search provider (lazy init)")
            search_provider = get_search_provider()
        if search_client is None:
            logger.debug("Resolving search client (lazy init)")
            search_client = get_search_client()
        if search_defaults is None:
            logger.debug("Resolving search defaults (lazy init)")
            search_defaults = get_search_defaults()
    except SearchConfigError as exc:
        logger.error("Search runtime initialization failed: %s", exc)
        if raise_on_error:
            raise
    except Exception as exc:
        logger.error("Unexpected error during search runtime init: %s", exc)
        if raise_on_error:
            raise

    return search_provider, search_client, search_defaults



# 提取requests/httpx里面的TIMEOUT EXCEPTION class, 这个异常用来排查问题非常有用
try:
    import requests
    _REQUESTS_TIMEOUT_EXC = (requests.exceptions.Timeout,)
except Exception:
    _REQUESTS_TIMEOUT_EXC = tuple()

try:
    import httpx
    _HTTPX_TIMEOUT_EXC = (httpx.TimeoutException,)
except Exception:
    _HTTPX_TIMEOUT_EXC = tuple()

_TIMEOUT_EXCEPTIONS = (TimeoutError,) + _REQUESTS_TIMEOUT_EXC + _HTTPX_TIMEOUT_EXC



# ===== SEARCH FUNCTIONS =====

def _resolve_search_runtime(client=None, provider=None, defaults=None, *, raise_on_error: bool = True):
    """解析 search provider/client/defaults参数，缓存由 search_factory 处理。
    raise_on_error：可以允许选择不抛出异常。默认情况下，会抛出。
    """
    runtime_provider, runtime_client, runtime_defaults = _ensure_search_runtime(raise_on_error=raise_on_error)
    resolved_provider = provider or runtime_provider
    resolved_client = client or runtime_client
    resolved_defaults = defaults or runtime_defaults

    return resolved_provider, resolved_client, resolved_defaults


def tavily_search_multiple(
    search_queries: List[str],
    max_results: Optional[int] = 3,
    topic: Optional[Literal["general", "news", "finance"]] = "general",
    include_raw_content: Optional[bool] = True,
    client=None,
    provider=None,
    defaults=None,
    timeout_seconds: Optional[int] = None,
) -> List[dict]:
    """根据搜索参数执行多个query的检索"""

    # 获取 search provider, search client, defaults参数
    provider, client, defaults_obj = _resolve_search_runtime(client, provider, defaults)

    if provider is None or client is None or defaults_obj is None:
        logger.error(
            "Search runtime not initialized (provider=%s, client=%s, defaults=%s)",
            bool(provider),
            bool(client),
            bool(defaults_obj),
        )
        raise SearchConfigError("Search runtime unavailable: provider/client/defaults could not be resolved")

    # 如果调用没有设置这几个参数，就使用默认参数
    effective_max_results = max_results if max_results is not None else defaults_obj.get("max_results", 3)
    effective_topic = topic if topic is not None else defaults_obj.get("topic", "general")
    effective_include_raw = include_raw_content if include_raw_content is not None else True
    effective_timeout = timeout_seconds if timeout_seconds is not None else defaults_obj.get("timeout_seconds")

    # 调用搜索函数，注：这里也可以使用AsyncTavilyClient实现并行调用
    search_docs = []
    for query in search_queries:
        started_at = time.time()
        try:
            result = provider.search(
                client,
                query,
                max_results=effective_max_results,
                include_raw_content=effective_include_raw,
                topic=effective_topic,
                timeout_seconds=effective_timeout,
            )
        except _TIMEOUT_EXCEPTIONS as exc:
            _record_search_metric(
                provider, query, effective_topic, effective_max_results,
                started_at, success=False, result_count=None, error_message=str(exc),
            )
            logger.error(
                "Search timeout for query='%s' topic='%s' timeout=%s: %s",
                query,
                effective_topic,
                effective_timeout,
                exc,
            )
            raise
        except Exception as exc:
            _record_search_metric(
                provider, query, effective_topic, effective_max_results,
                started_at, success=False, result_count=None, error_message=str(exc),
            )
            logger.error(
                "Search execution failed for query='%s' backend topic='%s': %s",
                query,
                effective_topic,
                exc,
            )
            raise

        _record_search_metric(
            provider, query, effective_topic, effective_max_results,
            started_at, success=True,
            result_count=_count_search_results(result),
            error_message=None,
            result=result,
        )
        search_docs.append(result)

    return search_docs


def _record_budget_decision(decision, query: str, input_sha1: str | None = None) -> None:
    """旁路记录一次 context budget 决策（best-effort，绝不影响总结）。

    ``input_sha1``：本次 summarize 的输入内容哈希（E4 观测，用于统计同一
    run 内的重复总结率；只记哈希，不记原文）。
    """
    try:
        if decision is None:
            return
        collector = get_baseline_collector()
        if collector is None:
            return
        collector.record_budget(
            query=(query or "")[:200],
            query_hash=_normalized_query_hash(query),
            input_sha1=input_sha1,
            **decision.to_fields(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("budget metric 记录失败（已忽略）: %s", exc)


def _count_search_results(result) -> int | None:
    """统计一次搜索返回的结果条数；结构不符时不猜，返回 None。"""
    try:
        if isinstance(result, dict):
            items = result.get("results")
            if isinstance(items, list):
                return len(items)
    except Exception:
        pass
    return None


def _sha1_12(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", errors="replace")).hexdigest()[:12]


def _normalized_query_hash(query: str) -> str:
    """规范化 query 的哈希（小写 + 折叠空白）—— 用于 run 内重复查询统计。"""
    return _sha1_12(" ".join((query or "").lower().split()))


def _canonical_url_key(url: str) -> str:
    """URL 规范化哈希（scheme + host(小写) + path，去 query/fragment）。

    只记录哈希，不落盘 URL 原文。
    """
    try:
        parsed = urlparse(url or "")
        canonical = f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
        return _sha1_12(canonical)
    except Exception:
        return _sha1_12(url or "")


def _result_hash_keys(result) -> tuple[list[str] | None, list[str] | None]:
    """从搜索结果中提取 (url_keys, content_sha1s)（E4 观测）。"""
    try:
        items = result.get("results") if isinstance(result, dict) else None
        if not isinstance(items, list):
            return None, None
        url_keys = [
            _canonical_url_key(str(item.get("url") or ""))
            for item in items if isinstance(item, dict)
        ]
        content_hashes = [
            _sha1_12(str(item.get("raw_content") or item.get("content") or ""))
            for item in items if isinstance(item, dict)
        ]
        return url_keys, content_hashes
    except Exception:
        return None, None


def _record_search_metric(
    provider,
    query: str,
    topic: str | None,
    max_results: int | None,
    started_at: float,
    *,
    success: bool,
    result_count: int | None,
    error_message: str | None,
    result=None,
) -> None:
    """旁路记录一次 search 调用的指标（baseline 观测用，失败不影响业务）。"""
    try:
        collector = get_baseline_collector()
        if collector is None:
            return
        url_keys, content_sha1s = _result_hash_keys(result) if result is not None else (None, None)
        collector.record_search(
            provider=type(provider).__name__,
            query=query,
            query_hash=_normalized_query_hash(query),
            topic=topic,
            max_results=max_results,
            started_at=started_at,
            finished_at=time.time(),
            success=success,
            result_count=result_count,
            url_keys=url_keys,
            content_sha1s=content_sha1s,
            error_message=(error_message or "")[:500] or None,
        )
    except Exception as exc:  # noqa: BLE001  # 观测绝不影响搜索/任务
        logger.debug("search metric 记录失败（已忽略）: %s", exc)



def summarize_webpage_content(webpage_content: str, query: str = "") -> str:
    """对一个网页的长文档进行总结

    P1：发送前应用 context budget（prompt + reserved_output + safety ≤ limit），
    长内容按 query 相关度确定性裁剪 —— 正常路径不再依赖 400 兜底。

    Args:
        webpage_content: 网页原始内容
        query: 研究 query / claim（用于裁剪时的相关性排序，可选）

    Returns:
        summary和key excerpts
    """
    try:
        # 预算裁剪（拿不到 limit 的 provider / 计算失败时原样返回）
        from deep_research.context_budget import fit_prompt_for_role

        try:
            prompt_text, decision = fit_prompt_for_role(
                role="researcher_summarizer",
                build_prompt=lambda c: SUMMARIZE_PROMPT.format(
                    webpage_content=c, date=get_today_str()
                ),
                content=webpage_content,
                query=query,
            )
        except Exception as exc:  # noqa: BLE001  # 预算失败不得阻断总结
            logger.warning("context budget 应用失败（按原样发送）: %s", exc)
            prompt_text = SUMMARIZE_PROMPT.format(
                webpage_content=webpage_content, date=get_today_str()
            )
            decision = None
        _record_budget_decision(decision, query, input_sha1=_sha1_12(webpage_content or ""))

        # 直接调用模型
        response = summarization_model.invoke([
            HumanMessage(content=prompt_text)
        ])

        # 手动解析 JSON
        data = parse_json_response(response.content)
        # key_excerpts 可能是数组或字符串，统一转为字符串
        if isinstance(data.get("key_excerpts"), list):
            data["key_excerpts"] = ", ".join(data["key_excerpts"])
        summary = Summary(**data)

        # 格式化summary和key_excerpts
        formatted_summary = (
            f"<summary>\n{summary.summary}\n</summary>\n\n"
            f"<key_excerpts>\n{summary.key_excerpts}\n</key_excerpts>"
        )

        return formatted_summary

    except Exception as e:
        logger.error(f"Failed to summarize webpage: {str(e)}")
        # 如果报错，就取文档前1000字
        return webpage_content[:DEFAULT_MAX_CONTEXT] + "..."


def deduplicate_search_results(search_results: List[dict]) -> dict:
    """对urls去重，以免处理重复文档

    Args:
        search_results: 搜索结果列表List[Dict] 

    Returns:
        去重后的搜索结果Dict
    """
    unique_results = {}

    for response in search_results:
        for result in response['results']:
            url = result['url']
            if url not in unique_results:
                unique_results[url] = result

    return unique_results


def process_search_results(unique_results: dict, query: str = "") -> dict:
    """ 处理搜索结果（对raw content做summary）

    Args:
        unique_results: url去重后的search results
        query: 本次搜索的 query（传给 summary 的 context budget 做相关性裁剪）

    Returns:
        做完summary后的results
    """
    summarized_results = {}

    for url, result in unique_results.items():
        # Use existing content if no raw content for summarization
        if not result.get("raw_content"):
            content = result['content']
        else:
            # Summarize raw content for better processing
            content = summarize_webpage_content(result['raw_content'][:MAX_CONTEXT_LENGTH], query=query)

        summarized_results[url] = {
            'title': result['title'],
            'content': content
        }

    return summarized_results


def format_search_output(summarized_results: dict) -> str:
    """对summarize后的结果做格式化（选择title, url, summary三个核心字段）

    Args:
        summarized_results: summarize后的results 

    Returns:
        格式化后的输出 
    """
    if not summarized_results:
        return "No valid search results found. Please try different search queries or use a different search API."

    formatted_output = "Search results: \n\n"

    for i, (url, result) in enumerate(summarized_results.items(), 1):
        formatted_output += f"\n\n--- SOURCE {i}: {result['title']} ---\n"
        formatted_output += f"URL: {url}\n\n"
        formatted_output += f"SUMMARY:\n{result['content']}\n\n"
        formatted_output += "-" * 80 + "\n"

    return formatted_output


# ===== RESEARCH TOOLS =====

def tavily_search(
    query: str,
    max_results: Annotated[Optional[int], InjectedToolArg] = None,
    topic: Annotated[Optional[Literal["general", "news", "finance"]], InjectedToolArg] = None,
) -> str:
    """根据配置好的tavily API去web上搜索结果，并返回做完网页内容摘要后的结果

    Args:
        query: 搜索关键词(query).
        max_results: 返回的最大结果数量(可选参数)，默认是3
        topic: 搜索主题，参数可以为：general, news, finance,（可选参数），默认是"general"

    Returns:
        格式化后并且去重+做完网页内容摘要的结果
    """
    # 获取默认参数
    _, _, defaults = _ensure_search_runtime()
    if defaults is None:
        raise SearchConfigError("Search defaults unavailable; search runtime not initialized")

    # 获取搜索参数
    resolved_max_results = max_results if max_results is not None else defaults.get("max_results", 3)
    resolved_topic = topic if topic is not None else defaults.get("topic", "general")
    include_raw_content = defaults.get("include_raw_content")
    if not include_raw_content:
        include_raw_content = True

    # 执行搜索并返回
    search_results = tavily_search_multiple(
        [query],  # 转换成列表
        max_results=resolved_max_results,
        topic=resolved_topic,
        include_raw_content=include_raw_content,
    )

    # 对搜索url去重
    unique_results = deduplicate_search_results(search_results)

    # 对网页长文档做摘要
    summarized_results = process_search_results(unique_results, query=query)

    # 格式化输出
    return format_search_output(summarized_results)


def think_tool(reflection: str) -> str:
    """用于对研究进展和决策进行策略反思的工具。
    每次搜索后，使用此工具分析结果并系统地规划下一步行动。这会在研究工作流程中故意暂停，以便进行质量决策。

    何时使用它:
    - 收到搜索结果后：我找到了哪些关键信息？
    - 在决定下一步之前：我的答案是否足够全面？
    - 在评估研究空白时：哪些关键信息仍然缺失？
    - 在结束研究之前：我现在能提供一个完整的答案吗？

    反思内容应该强调以下这些方面：
    1. 当前研究的分析 — 我收集到了哪些具体信息？
    2. 差距评估 - 还缺少哪些关键信息？
    3. 质量评估 - 我是否有足够的证据/例子来提供一个好的答案？
    4. 战略决策 - 我应该继续搜索还是给出答案？

    Args:
        reflection：您对研究进展、发现、存在的差距以及下一步行动的详细反思。

    Returns:
        记录反思的内容，以供智能体决策参考
    """

    return f"Reflection recorded: {reflection}"



def refine_draft_report(research_brief: Annotated[str, InjectedToolArg], 
                        findings: Annotated[str, InjectedToolArg], 
                        draft_report: Annotated[str, InjectedToolArg]):

    """根据新的研究发现(findings)完善目前的报告草稿(draft_report)

    该工具会综合当前所有研究结果整理输出一份更全面的报告草稿。

    Args:
        research_brief：用户的研究请求。
        findings：针对用户请求收集的研究结果。
        draft_report：基于研究结果和用户请求的报告草稿。

    Returns:
        精炼后的报告草案
    """

    # 组装提示词
    draft_report_prompt = REFINE_DRAFT_REPORT_PROMPT.format(
        research_brief=research_brief,
        findings=findings,
        draft_report=draft_report,
        date=get_today_str()
    )

    # 调用大模型来修正
    draft_report_obj = writer_model.invoke([HumanMessage(content=draft_report_prompt)])

    # 如果返回是message则抽取content字段，否则直接返回
    return getattr(draft_report_obj, "content", draft_report_obj)


# 注册成LangChain工具

# 搜索tool
_tavily_search_tool = tool(parse_docstring=True)(tavily_search)

# 反思tool
_think_tool = tool(parse_docstring=True)(think_tool)

# 精修tool
_refine_draft_report_tool = tool(parse_docstring=True)(refine_draft_report)

