"""项目根目录解析。

不依赖进程当前工作目录（cwd），因此从任意目录启动 uvicorn / pytest / notebook
都能得到一致结果。
"""

from __future__ import annotations

import os
from pathlib import Path

# 用于识别项目根的标记文件/目录（按优先级）
_ROOT_MARKERS = ("pyproject.toml", "config.server.example.yml", ".git")


def find_project_root() -> Path:
    """定位项目根目录。

    优先级：
        1. ``DEEP_RESEARCH_HOME`` 环境变量（显式覆盖）
        2. 从本文件向上逐级查找，命中标记文件的那一层
        3. 回退到包的上一级（即 ``deep_research/`` 所在目录）
    """
    env_home = os.environ.get("DEEP_RESEARCH_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if any((parent / marker).exists() for marker in _ROOT_MARKERS):
            return parent

    # 未命中任何标记：回退到包目录的上一级，而非包目录本身
    return here.parent.parent
