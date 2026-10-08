"""Web 层配置。

只承载 backend 特有的字段（CORS、限流等）。环境标识与路径统一委托给
:class:`deep_research.settings.EngineSettings`，避免出现两个真相源。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from deep_research.settings import EngineSettings, get_engine_settings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    debug: bool = Field(False, validation_alias="DEBUG")
    api_prefix: str = Field("/api", validation_alias="API_PREFIX")

    # 列表型环境变量用 JSON 语法传入，例如 CORS_ORIGINS='["https://a.example"]'
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        validation_alias="CORS_ORIGINS",
    )

    rate_limit_window: int = Field(60, validation_alias="RATE_LIMIT_WINDOW")
    rate_limit_max: int = Field(30, validation_alias="RATE_LIMIT_MAX")

    # ===== Phase G Worker Runtime =====
    # 所有时间参数集中在此，不散落 magic number（执行包 §7 / §30）。
    # 测试通过环境变量注入短参数，避免真实等待几十秒。

    # claim TTL 必须 >= 3 × heartbeat interval（§7）
    heartbeat_interval_seconds: float = Field(10.0, validation_alias="HEARTBEAT_INTERVAL_SECONDS")
    claim_ttl_seconds: float = Field(30.0, validation_alias="CLAIM_TTL_SECONDS")

    # 仅用于抑制瞬时重复投递，不是安全边界（§8.3）
    job_dedupe_ttl_seconds: float = Field(5.0, validation_alias="JOB_DEDUPE_TTL_SECONDS")

    # 每任务事件流的最大保留条数（§9.5）
    event_retention_maxlen: int = Field(1000, validation_alias="EVENT_RETENTION_MAXLEN")

    worker_max_attempts: int = Field(3, validation_alias="WORKER_MAX_ATTEMPTS")

    # Orphan Reconciler：多久没动静才判定为孤儿（执行计划 §6）。
    # 有这道宽限期才不会与「刚入队、job 尚未被消费」的任务竞争。
    orphan_grace_seconds: float = Field(60.0, validation_alias="ORPHAN_GRACE_SECONDS")
    # 两次扫描之间的间隔
    orphan_sweep_interval_seconds: float = Field(
        60.0, validation_alias="ORPHAN_SWEEP_INTERVAL_SECONDS"
    )
    worker_backoff_base_seconds: float = Field(1.0, validation_alias="WORKER_BACKOFF_BASE_SECONDS")

    @property
    def claim_ttl_ms(self) -> int:
        return int(self.claim_ttl_seconds * 1000)

    @property
    def heartbeat_interval_ms(self) -> int:
        return int(self.heartbeat_interval_seconds * 1000)

    # ===== 引擎配置透传 =====

    @property
    def engine(self) -> EngineSettings:
        return get_engine_settings()

    @property
    def data_dir(self) -> Path:
        return self.engine.resolved_data_dir

    @property
    def log_dir(self) -> Path:
        return self.engine.resolved_log_dir

    @property
    def config_path(self) -> Path:
        return self.engine.resolved_config_path

    @property
    def is_test(self) -> bool:
        return self.engine.is_test

    @property
    def is_production(self) -> bool:
        return self.engine.is_production

    @property
    def offline(self) -> bool:
        return self.engine.offline


@lru_cache
def get_settings() -> Settings:
    """进程内缓存的 backend 配置单例。"""
    return Settings()


def reset_settings() -> None:
    """清除缓存，供测试隔离使用。"""
    get_settings.cache_clear()
