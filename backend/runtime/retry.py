"""失败分类与重试判定（执行包 §12）。

原则：**不要所有异常统一 retry**。

```text
可重试      基础设施瞬时故障、provider 瞬时错误、DB 锁竞争
不可重试    领域错误、状态非法、用户取消、配置错误
```

不可重试的错误若被盲目重试，只会把同一次失败重复消费若干次——
对 LLM 服务意味着重复计费，对状态机意味着掩盖真实缺陷。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    code: str


# 明确的领域错误 —— 绝不重试
_DOMAIN_CODES = {
    "InvalidTaskTransition": "INVALID_TASK_TRANSITION",
    "InvalidReviewState": "INVALID_REVIEW_STATE",
    "UnknownTaskStatus": "UNKNOWN_TASK_STATUS",
    # Phase 5B：投机研究 fence 不一致 = 状态被污染，重试只会掩盖问题
    "ResearchFenceError": "RESEARCH_FENCE_ERROR",
}

# 配置 / 基础设施永久性错误
_PERMANENT_CODES = {
    "CheckpointerError": "CHECKPOINTER_ERROR",
    "UnsupportedDatabaseBackend": "UNSUPPORTED_DATABASE_BACKEND",
    "RedisUnavailable": "REDIS_UNAVAILABLE",
    "EnqueueRejected": "ENQUEUE_REJECTED",
}

# 判定为「瞬时」的异常名或消息特征
_TRANSIENT_HINTS = (
    "timeout", "timed out", "rate limit", "ratelimit", "429",
    "connection", "temporarily", "unavailable", "503", "502", "504",
    "lock", "database is locked", "reset by peer",
)


def classify(exc: BaseException) -> RetryDecision:
    """判定异常是否可重试。"""
    name = type(exc).__name__

    if name in _DOMAIN_CODES:
        return RetryDecision(False, _DOMAIN_CODES[name])

    if name in _PERMANENT_CODES:
        return RetryDecision(False, _PERMANENT_CODES[name])

    # 用户取消：不是失败，绝不重试
    if name in ("TaskCancelled", "CancelledError"):
        return RetryDecision(False, "TASK_CANCELLED")

    message = f"{name}: {exc}".lower()
    if any(hint in message for hint in _TRANSIENT_HINTS):
        return RetryDecision(True, "TRANSIENT_ERROR")

    # 未识别的异常默认**不重试** —— 宁可显式失败，也不要盲目重复
    # 一个可能有副作用（LLM 计费）的操作。
    return RetryDecision(False, "UNCLASSIFIED_ERROR")


__all__ = ["RetryDecision", "classify"]
