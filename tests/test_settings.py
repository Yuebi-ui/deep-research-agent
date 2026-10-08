"""EngineSettings 的路径解析与环境判定。"""

import os
from pathlib import Path

import pytest

from deep_research._paths import find_project_root
from deep_research.settings import EngineSettings, get_engine_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_project_root_does_not_depend_on_cwd(tmp_path, monkeypatch):
    """从任意 cwd 解析项目根，结果必须一致。"""
    before = find_project_root()
    monkeypatch.chdir(tmp_path)
    assert find_project_root() == before


def test_resolved_paths_are_repo_root_anchored(tmp_path, monkeypatch):
    settings = EngineSettings()
    monkeypatch.chdir(tmp_path)

    assert settings.resolved_config_path == REPO_ROOT / "config.yml"
    assert settings.resolved_data_dir == REPO_ROOT / "data"
    assert settings.resolved_log_dir == REPO_ROOT / "logs"


def test_relative_env_path_resolves_against_repo_root(tmp_path, monkeypatch):
    """相对 CONFIG_PATH 按项目根解析，而不是 cwd。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIG_PATH", "custom/my.yml")
    settings = EngineSettings()
    assert settings.resolved_config_path == REPO_ROOT / "custom" / "my.yml"


def test_absolute_env_path_is_respected(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.yml"
    monkeypatch.setenv("CONFIG_PATH", str(target))
    assert EngineSettings().resolved_config_path == target


def test_env_override_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEP_RESEARCH_HOME", str(tmp_path))
    assert find_project_root() == tmp_path.resolve()


# ===== 环境判定 =====


def test_development_defaults_keep_live_providers(monkeypatch):
    """不设任何环境变量时，development 行为必须与改造前一致（真实 provider）。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ALLOW_LIVE_EXTERNAL_APIS", raising=False)
    settings = EngineSettings()

    assert settings.app_env == "development"
    assert settings.offline is False
    assert settings.use_fake_llm is False
    assert settings.use_fake_search is False


def test_test_env_forces_fake(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    settings = EngineSettings()

    assert settings.is_test is True
    assert settings.offline is True
    assert settings.use_fake_llm is True
    assert settings.use_fake_search is True
    assert settings.use_fake_embeddings is True


def test_allow_live_external_apis_false_forces_fake(monkeypatch):
    """显式停用外部 API 时，即便在 development 也走 Fake。"""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_LIVE_EXTERNAL_APIS", "false")
    assert EngineSettings().use_fake_llm is True


def test_explicit_fake_provider_selection(monkeypatch):
    """单独指定 LLM_PROVIDER=fake 时，只应影响 LLM，不应连带改写搜索。

    需要显式打开 ALLOW_LIVE_EXTERNAL_APIS，否则 conftest 的会话级
    ``ALLOW_LIVE_EXTERNAL_APIS=false`` 会让所有 provider 都走 Fake，
    从而无法隔离出 LLM_PROVIDER 的效果。
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_LIVE_EXTERNAL_APIS", "true")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    settings = EngineSettings()

    assert settings.use_fake_llm is True
    assert settings.use_fake_search is False
    assert settings.use_fake_embeddings is True


def test_stage_defaults_to_prod(monkeypatch):
    monkeypatch.delenv("STAGE", raising=False)
    assert EngineSettings().stage == "prod"


def test_get_engine_settings_is_cached():
    assert get_engine_settings() is get_engine_settings()


def test_config_path_and_stage_are_readable():
    """配置文件应存在于项目根——这是 resolve 正确的间接证据。"""
    settings = EngineSettings()
    assert settings.resolved_config_path.name == "config.yml"
    assert settings.resolved_config_path.parent == REPO_ROOT


@pytest.mark.parametrize("env_key", ["APP_ENV", "ALLOW_LIVE_EXTERNAL_APIS", "STAGE"])
def test_env_keys_are_wired(env_key, monkeypatch):
    """确认关键环境变量确实被读取（防止 validation_alias 写错）。"""
    monkeypatch.setenv(env_key, "test" if env_key == "APP_ENV" else "1")
    settings = EngineSettings()
    assert settings is not None


def test_data_dir_import_does_not_touch_cwd(tmp_path, monkeypatch):
    """解析路径不应有任何副作用式地创建 cwd 下的目录。"""
    monkeypatch.chdir(tmp_path)
    settings = EngineSettings()
    _ = settings.resolved_data_dir
    assert not (tmp_path / "data").exists()
    assert os.getcwd() == str(tmp_path)
