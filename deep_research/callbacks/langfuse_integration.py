"""Langfuse 可观测性集成（可选启用）。

使用方法：
    1. pip install langfuse
    2. 在 config.yml 中配置 langfuse 参数
    3. 设置环境变量 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY

如果 Langfuse 未安装或未配置，所有操作静默跳过（不影响正常功能）。
"""

from __future__ import annotations

from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

_langfuse_available = False
_langfuse_client: object | None = None

try:
    from langfuse import Langfuse

    _langfuse_available = True
except ImportError:
    logger.debug("langfuse not installed — tracing disabled")


class LangfuseTracer:
    """轻量 Langfuse 追踪包装器。"""

    def __init__(self, public_key: str = "", secret_key: str = "", host: str = "") -> None:
        self._client = None
        self._trace = None
        self._enabled = False

        if not _langfuse_available:
            return

        import os

        pk = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        h = host or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if pk and sk:
            try:
                self._client = Langfuse(public_key=pk, secret_key=sk, host=h)
                self._enabled = True
                logger.info("Langfuse tracer initialized")
            except Exception as e:
                logger.warning("Failed to init Langfuse: %s", e)

    def start_trace(self, name: str, thread_id: str = "", metadata: dict | None = None) -> None:
        if not self._enabled or not self._client:
            return
        try:
            self._trace = self._client.trace(
                name=name,
                id=thread_id,
                metadata=metadata or {},
            )
        except Exception as e:
            logger.debug("Langfuse trace start failed: %s", e)

    def log_node(self, name: str, input_data: str = "", output_data: str = "", metadata: dict | None = None) -> None:
        if not self._enabled or not self._trace:
            return
        try:
            self._trace.span(
                name=name,
                input={"content": input_data[:500]},
                output={"content": output_data[:500]},
                metadata=metadata or {},
            )
        except Exception as e:
            logger.debug("Langfuse span failed: %s", e)

    def log_score(self, name: str, value: float, comment: str = "") -> None:
        if not self._enabled or not self._trace:
            return
        try:
            self._trace.score(name=name, value=value, comment=comment)
        except Exception as e:
            logger.debug("Langfuse score failed: %s", e)

    def end_trace(self) -> None:
        if not self._enabled or not self._client:
            return
        try:
            self._client.flush()
        except Exception as e:
            logger.debug("Langfuse flush failed: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled
