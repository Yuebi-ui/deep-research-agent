"""离线 Fake 搜索 provider。

按 ``deep_research.providers.<backend>`` 的动态导入约定命名：当 backend 为
``fake`` 时，``search_factory._maybe_import_provider`` 会自动导入本模块并
通过模块级 ``PROVIDER`` 属性完成注册。

⚠️ 这里刻意**不要**从 search_factory 导入 ``register_provider``：
``_maybe_import_provider`` 会把模块内同名的属性当作注册 hook，调用
``register_fn(register_provider)``，从而与原函数签名冲突。``PROVIDER``
属性是更干净且无歧义的约定。

返回结构需与 Tavily 保持一致，因为下游 ``deduplicate_search_results`` /
``process_search_results`` 依赖 ``response["results"]`` 及每条记录的
``url`` / ``title`` / ``content`` / ``raw_content`` 字段。
"""

from __future__ import annotations

from typing import Any

from deep_research.tools.search_factory import SearchProvider

# 使用 .invalid 顶级域（RFC 2606 保留，保证永远不可解析），
# 万一被真实发起请求也不会打到外部服务
_FIXTURE_URL = "https://source-{index}.example.invalid/doc"


class FakeSearchProvider(SearchProvider):
    """确定性的离线搜索结果。"""

    def build_client(self, provider_cfg: dict[str, Any]) -> Any:
        # 无需任何凭据或网络客户端
        return {"offline": True}

    def search(
        self,
        client: Any,
        query: str,
        *,
        max_results: int,
        include_raw_content: bool,
        topic: str,
        timeout_seconds: int | None,
    ) -> Any:
        results = []
        for index in range(1, max(1, max_results) + 1):
            results.append(
                {
                    "url": _FIXTURE_URL.format(index=index),
                    "title": f"Fake Source {index} for '{query}'",
                    "content": f"Deterministic offline content {index} about {query}.",
                    "raw_content": (
                        f"Deterministic offline raw content {index} about {query}. "
                        "No external service was contacted."
                    ),
                    "score": 1.0 - index * 0.01,
                }
            )
        return {"query": query, "results": results}

    def defaults(self, provider_cfg: dict[str, Any]) -> dict[str, Any]:
        defaults = {
            "max_results": 3,
            "topic": "general",
            "include_raw_content": True,
            "timeout_seconds": None,
        }
        defaults.update({k: provider_cfg.get(k, defaults[k]) for k in defaults})
        return defaults


# 由 search_factory._maybe_import_provider 通过该属性自注册
PROVIDER = FakeSearchProvider()
