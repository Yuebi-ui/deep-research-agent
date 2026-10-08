"""Import 冒烟测试。

用途：捕获「模块路径写错」这类只有真正 import 才会暴露的问题。

起因：`deep_research/providers/customsearch.py` 长期从
`deep_research.search_factory` 导入（该路径不存在，真实路径是
`deep_research.tools.search_factory`）。由于该模块只在 search backend 被设为
`customsearch` 时才动态加载，错误一直潜伏（V3 §3.1）。

本文件遍历导入全部模块，确保任何同类问题会在 CI 直接失败。
"""

import importlib
import pkgutil

import pytest


def _iter_submodules(package_name: str) -> list[str]:
    package = importlib.import_module(package_name)
    found = []
    for module_info in pkgutil.walk_packages(
        package.__path__, prefix=f"{package_name}."
    ):
        found.append(module_info.name)
    return sorted(found)


# 全部 deep_research 子模块
DEEP_RESEARCH_MODULES = _iter_submodules("deep_research")
# backend 子模块（排除需要真实外部资源的）
BACKEND_MODULES = [
    m for m in _iter_submodules("backend")
    if not m.startswith(("backend.core",))  # core 已被 conftest 覆盖，但一并导入无妨
]


def test_deep_research_has_expected_provider_modules():
    """防止 provider 目录被整体删除或重命名后测试静默通过。"""
    names = set(DEEP_RESEARCH_MODULES)
    assert "deep_research.providers.customsearch" in names
    assert "deep_research.providers.fake" in names


@pytest.mark.parametrize("module_name", DEEP_RESEARCH_MODULES)
def test_deep_research_module_imports(module_name):
    """每个 deep_research 子模块都必须能在离线环境下导入。

    `deep_research.testing.fake_llm` 的 `asyncio` 等辅助模块、
    `deep_research.mcp.tool_server` 也在其中——后者只做导入，不启动 server。
    """
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", BACKEND_MODULES)
def test_backend_module_imports(module_name):
    importlib.import_module(module_name)


def test_customsearch_provider_module_is_importable():
    """回归测试：该模块曾经因为错误的 import 路径而永远无法导入。"""
    module = importlib.import_module("deep_research.providers.customsearch")

    assert hasattr(module, "CustomSearchProvider")
    assert hasattr(module, "PROVIDER")
    assert isinstance(module.PROVIDER, module.CustomSearchProvider)


def test_customsearch_provider_is_registered():
    """验证模块的自注册副作用。

    注意必须先从 ``sys.modules`` 移除再导入：conftest 会在每个用例后恢复
    provider 注册表快照，而 Python 会缓存已导入模块——若直接
    ``import_module``，模块体不会重新执行，注册也就不会发生。
    """
    import sys

    from deep_research.tools.search_factory import _PROVIDER_REGISTRY

    sys.modules.pop("deep_research.providers.customsearch", None)
    importlib.import_module("deep_research.providers.customsearch")

    assert "customsearch" in _PROVIDER_REGISTRY


def test_all_provider_modules_expose_provider_attribute():
    """provider 模块统一通过 PROVIDER 属性自注册（见 V3 §3.1 与 fake.py 注释）。"""
    for name in ("deep_research.providers.fake", "deep_research.providers.customsearch"):
        module = importlib.import_module(name)
        assert getattr(module, "PROVIDER", None) is not None, name
