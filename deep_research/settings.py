"""引擎级配置。

只负责**运行时路径、环境标识与功能开关**；模型/搜索/角色的业务配置仍然由
``config.yml`` 承载（见 :func:`deep_research.utils.load_config`）。

放在 ``deep_research`` 而非 ``backend`` 内，以便引擎在 FastAPI、CLI、pytest、
notebook 中都能独立复用。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from deep_research._paths import find_project_root

AppEnv = Literal["development", "test", "production"]

# 将 provider 显式指定为 fake 时使用的取值
FAKE_PROVIDER = "fake"


class EngineSettings(BaseSettings):
    """引擎运行时配置。

    所有字段都可通过环境变量覆盖（见各字段的 ``validation_alias``），
    兼容项目既有的 ``STAGE`` / ``CONFIG_PATH`` / ``DEEP_RESEARCH_LOG_DIR``。
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    app_env: AppEnv = Field("development", validation_alias="APP_ENV")
    stage: str = Field("prod", validation_alias="STAGE")

    # 以下路径为 None 时回退到 ``项目根目录/<默认值>``
    config_path: Path | None = Field(None, validation_alias="CONFIG_PATH")
    data_dir: Path | None = Field(None, validation_alias="DR_DATA_DIR")
    log_dir: Path | None = Field(None, validation_alias="DEEP_RESEARCH_LOG_DIR")

    # 付费外部 API 的总开关。
    # 默认 True —— 保证不设任何环境变量时 development 行为与改造前完全一致。
    # 显式设为 false 可在任意环境强制走 Fake provider。
    allow_live_external_apis: bool = Field(
        True, validation_alias="ALLOW_LIVE_EXTERNAL_APIS"
    )

    # provider 选择："auto" 表示按 config.yml 正常解析
    llm_provider: str = Field("auto", validation_alias="LLM_PROVIDER")
    search_provider: str = Field("auto", validation_alias="SEARCH_PROVIDER")

    # ===== 派生路径 =====

    @property
    def project_root(self) -> Path:
        """项目根目录（与 cwd 无关）。"""
        return find_project_root()

    def _resolve(self, value: Path | None, default: str) -> Path:
        """把相对路径按项目根目录解析，而不是按 cwd。"""
        if value is None:
            return self.project_root / default
        return value if value.is_absolute() else self.project_root / value

    @property
    def resolved_config_path(self) -> Path:
        return self._resolve(self.config_path, "config.yml")

    @property
    def resolved_data_dir(self) -> Path:
        return self._resolve(self.data_dir, "data")

    @property
    def resolved_log_dir(self) -> Path:
        return self._resolve(self.log_dir, "logs")

    # ===== 环境判定 =====

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def offline(self) -> bool:
        """是否处于「禁止真实外部调用」状态。

        三条触发路径：

        1. ``APP_ENV=test`` —— 测试环境恒离线，这是防止 CI / 测试误调付费 API
           的主要闸门；
        2. ``ALLOW_LIVE_EXTERNAL_APIS=false`` —— 显式全局停用；
        3. 对应 provider 显式设为 ``fake``。
        """
        return self.is_test or not self.allow_live_external_apis

    @property
    def use_fake_llm(self) -> bool:
        return self.offline or self.llm_provider == FAKE_PROVIDER

    @property
    def use_fake_search(self) -> bool:
        return self.offline or self.search_provider == FAKE_PROVIDER

    @property
    def use_fake_embeddings(self) -> bool:
        return self.offline or self.llm_provider == FAKE_PROVIDER


@lru_cache
def get_engine_settings() -> EngineSettings:
    """进程内缓存的配置单例。"""
    return EngineSettings()


def reset_engine_settings() -> None:
    """清除缓存，供测试隔离使用。"""
    get_engine_settings.cache_clear()
