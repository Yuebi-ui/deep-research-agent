#!/usr/bin/env python3
"""交互式配置 Hybrid 运行所需的密钥与环境开关。

用法：
    .venv/bin/python scripts/configure_hybrid_secrets.py

本脚本只做四件事，不做别的：

  1. 用 getpass 读取 DashScope / Tavily 两个 key（终端不回显）；
  2. 写入 config.yml 的
         stages.prod.cognition.openai.api_key
         stages.prod.search.tavily.api_key
     （若 config.yml 还是 Fake 模板、缺少这两个路径，则先以
       config.hybrid.example.yml 为基线启用 Hybrid 配置）；
  3. 把 .env.server 切到真实运行模式：
         APP_ENV                    test  → development
         ALLOW_LIVE_EXTERNAL_APIS   false → true
         LLM_PROVIDER               fake  → auto
         SEARCH_PROVIDER            fake  → auto
  4. 只回显 SET / NOT SET 与开关值，**永不回显密钥内容**。

为什么必须改 LLM_PROVIDER / SEARCH_PROVIDER：
    见 deep_research/settings.py:108-116 —— use_fake_llm / use_fake_search /
    use_fake_embeddings 都是 `offline or provider == "fake"`。只把 APP_ENV
    改成 development 而把这两个留在 fake，E2E 会静默全走 Fake，
    真实 Local / Cloud 模型一次都不会被调用。

本脚本不启动任何服务、不调用任何 API、不访问网络。
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少 PyYAML。请用项目 venv 运行：.venv/bin/python scripts/configure_hybrid_secrets.py")

# ===== 路径 =====
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yml"
CONFIG_BAK = PROJECT_ROOT / "config.yml.bak"
ENV_PATH = PROJECT_ROOT / ".env.server"
ENV_BAK = PROJECT_ROOT / ".env.server.bak"
HYBRID_TEMPLATE = PROJECT_ROOT / "config.hybrid.example.yml"

STAGE = "prod"
DASHSCOPE_KEY_PATH = ("cognition", "openai", "api_key")
TAVILY_KEY_PATH = ("search", "tavily", "api_key")

# .env.server 中要切换的值（按你的要求 + provider 短路修复）
ENV_CHANGES = [
    ("APP_ENV", "development"),
    ("ALLOW_LIVE_EXTERNAL_APIS", "true"),
    ("LLM_PROVIDER", "auto"),
    ("SEARCH_PROVIDER", "auto"),
]

# ===== 输出脱敏：任何路径下的输出都不能带出密钥 =====
_SECRETS: list[str] = []


def emit(msg: object = "") -> None:
    """所有终端输出都经过这里，密钥一旦出现在文本中会被替换掉。"""
    text = str(msg)
    for secret in _SECRETS:
        # 过短的字符串做全局替换会误伤正常文本
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<REDACTED>")
    print(text)


def fail(msg: str) -> None:
    emit(f"✗ {msg}")
    sys.exit(1)


def section(title: str) -> None:
    emit(f"\n==== {title} ====")


# ===== YAML 路径定位（保留注释，不做 load/dump 往返）=====
_YAML_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.-]*):(?:\s|$)")


def locate_yaml_path(lines: list[str], target: tuple[str, ...]) -> list[int]:
    """返回以 ``stages.<STAGE>.<target>`` 为完整路径的行号（0-based）。

    只做缩进栈匹配，不改写内容——这样 config.hybrid.example.yml 里的
    注释（尤其是末尾的全 Cloud 回滚说明）可以完整保留。
    """
    full_target = ("stages", STAGE) + target
    stack: list[tuple[int, str]] = []
    hits: list[int] = []

    for lineno, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _YAML_KEY_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        key = m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if tuple(k for _, k in stack) == full_target:
            hits.append(lineno)
    return hits


_VALUE_RE = re.compile(r"^(\s*api_key:\s*)(.*?)(\s*#.*)?\s*$")


def set_yaml_secret(text: str, target: tuple[str, ...], secret: str) -> str:
    """把 YAML 中指定路径的 api_key 值换掉，保留缩进与行尾注释。"""
    lines = text.splitlines(keepends=True)
    hits = locate_yaml_path(lines, target)
    dotted = ".".join(("stages", STAGE) + target)

    if len(hits) == 0:
        fail(f"config.yml 中找不到 {dotted} —— 配置结构与预期不符，未做任何写入。")
    if len(hits) > 1:
        fail(f"config.yml 中 {dotted} 出现 {len(hits)} 次，无法确定改哪一处，未做任何写入。")

    lineno = hits[0]
    m = _VALUE_RE.match(lines[lineno].rstrip("\n"))
    if not m:
        fail(f"{dotted} 所在行的格式无法解析，未做任何写入。")

    # json.dumps 产出的双引号字符串是合法 YAML 标量，转义由标准库保证
    quoted = json.dumps(secret)
    lines[lineno] = f"{m.group(1)}{quoted}{m.group(3) or ''}\n"
    return "".join(lines)


def yaml_get(data: object, path: tuple[str, ...]) -> object:
    node = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING = object()


def diff_paths(a: object, b: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """只返回发生变化的路径，**不返回值**（避免把密钥带进日志）。"""
    out: list[tuple[str, ...]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in set(a) | set(b):
            if key not in a or key not in b:
                out.append(prefix + (str(key),))
                continue
            out.extend(diff_paths(a[key], b[key], prefix + (str(key),)))
    elif a != b:
        out.append(prefix)
    return out


# ===== 文件读写 =====
def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """原子写入：临时文件 0600，成功后再 replace，避免半截文件。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_once(path: Path, bak: Path) -> str:
    """首次备份优先：.bak 已存在就保留它，绝不覆盖既有备份。"""
    if bak.exists():
        return f"已存在，保留原有备份（{bak.stat().st_size} bytes，未覆盖）"
    if not path.exists():
        return "源文件不存在，跳过"
    bak.write_bytes(path.read_bytes())
    os.chmod(bak, 0o600)
    return f"已创建（{bak.stat().st_size} bytes）"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf8")


def parse_yaml(text: str, label: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML 的报错会带出错行的原文片段，其中可能含密钥 —— 只报位置
        mark = getattr(exc, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行附近）" if mark is not None else ""
        fail(f"{label} 不是合法 YAML{where}，未做任何写入。")


# ===== 交互输入 =====
def prompt_secret(label: str, expect_prefix: str) -> str:
    emit(f"\n{label}")
    emit(f"  （输入不回显；粘贴后直接回车。预期以 {expect_prefix} 开头）")
    while True:
        value = getpass.getpass(f"  {label} > ").strip()
        if not value:
            emit("  ✗ 输入为空，请重新输入。")
            continue
        if not value.startswith(expect_prefix):
            emit(f"  ! 注意：这个值不以 {expect_prefix} 开头，确认没贴错再继续。")
        return value


# ===== .env.server 处理 =====
def set_dotenv_value(text: str, key: str, value: str) -> tuple[str, str]:
    """设置 dotenv 变量。返回 (新文本, 变化描述)。只改未注释的赋值行。"""
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]

    if len(hits) > 1:
        fail(f".env.server 中 {key} 有多处未注释的赋值，无法确定改哪一处，未做任何写入。")

    if not hits:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"# 由 configure_hybrid_secrets.py 追加\n{key}={value}\n")
        return "".join(lines), f"{key}: (新增) → {value}"

    old = lines[hits[0]].strip()
    new_line = f"{key}={value}\n"
    if old == f"{key}={value}":
        return "".join(lines), f"{key}: {value}（已是目标值，未改动）"
    lines[hits[0]] = new_line
    return "".join(lines), f"{key}: {old.split('=', 1)[1]} → {value}"


# ===== 主流程 =====
def main() -> int:
    section("0. 前置检查")
    for required in (CONFIG_PATH, ENV_PATH):
        if not required.exists():
            fail(f"找不到 {required}")
        emit(f"· {required.relative_to(PROJECT_ROOT)} 存在")

    config_text = read_text(CONFIG_PATH)
    config_data = parse_yaml(config_text, "config.yml")
    env_text = read_text(ENV_PATH)

    # ---- config.yml 是否已经是 Hybrid 结构 ----
    bootstrap = False
    current_dashscope = yaml_get(config_data, ("stages", STAGE) + DASHSCOPE_KEY_PATH)
    current_tavily = yaml_get(config_data, ("stages", STAGE) + TAVILY_KEY_PATH)
    if current_dashscope is _MISSING or current_tavily is _MISSING:
        bootstrap = True
        emit("! 当前 config.yml 还是 Fake 模板，缺少 cognition.openai / search.tavily。")
        if not HYBRID_TEMPLATE.exists():
            fail("缺少 config.hybrid.example.yml，无法启用 Hybrid 配置。")
        emit(f"· 将先以 {HYBRID_TEMPLATE.name} 为基线启用 Hybrid 配置（原文件会备份）")

    section("1. 备份")
    emit(f"· config.yml.bak  {backup_once(CONFIG_PATH, CONFIG_BAK)}")
    emit(f"· .env.server.bak {backup_once(ENV_PATH, ENV_BAK)}")

    section("2. 输入密钥")
    dashscope_key = prompt_secret("DashScope API Key", "sk-")
    tavily_key = prompt_secret("Tavily API Key", "tvly-")
    _SECRETS.extend([dashscope_key, tavily_key])

    if dashscope_key == tavily_key:
        fail("两个 key 输入完全相同 —— 大概率贴错了，未做任何写入。")

    section("3. 写入 config.yml")

    if bootstrap:
        base_text = read_text(HYBRID_TEMPLATE)
        base_data = parse_yaml(base_text, "config.hybrid.example.yml")
        for path in (DASHSCOPE_KEY_PATH, TAVILY_KEY_PATH):
            if yaml_get(base_data, ("stages", STAGE) + path) is _MISSING:
                fail(f"Hybrid 模板缺少 {'/'.join(path)}，未做任何写入。")
        baseline = base_data
        emit(f"· 基线：{HYBRID_TEMPLATE.name}（Fake 配置已备份到 config.yml.bak）")
    else:
        baseline = config_data
        base_text = config_text
        emit("· 基线：当前 config.yml（已含 Hybrid 结构，仅替换两个 key）")

    patched = set_yaml_secret(base_text, DASHSCOPE_KEY_PATH, dashscope_key)
    patched = set_yaml_secret(patched, TAVILY_KEY_PATH, tavily_key)

    # ---- 写入前校验：只有这两个路径允许变化 ----
    patched_data = parse_yaml(patched, "写入后的 config.yml")
    # baseline / patched_data 都是整份 config 字典，diff_paths 返回的已是完整路径
    changed = sorted(".".join(p) for p in diff_paths(baseline, patched_data))
    expected = sorted(".".join(("stages", STAGE) + p) for p in (DASHSCOPE_KEY_PATH, TAVILY_KEY_PATH))
    if changed != expected:
        emit("✗ 写入前校验失败：改动范围与预期不符。")
        emit(f"  预期改动：{expected}")
        emit(f"  实际改动：{changed}")  # 只有路径名，没有值
        fail("已中止，config.yml 未被修改。")
    if yaml_get(patched_data, ("stages", STAGE) + DASHSCOPE_KEY_PATH) != dashscope_key:
        fail("DashScope key 回读不一致，已中止，config.yml 未被修改。")
    if yaml_get(patched_data, ("stages", STAGE) + TAVILY_KEY_PATH) != tavily_key:
        fail("Tavily key 回读不一致，已中止，config.yml 未被修改。")

    atomic_write(CONFIG_PATH, patched, mode=0o600)
    emit("· 已写入 config.yml（权限 0600）")
    emit(f"· 注释保留检查：{'是' if base_text.count('#') == patched.count('#') else '否（请检查）'}")

    section("4. 写入 .env.server")
    new_env = env_text
    reports = []
    for key, value in ENV_CHANGES:
        new_env, report = set_dotenv_value(new_env, key, value)
        reports.append(report)
    for report in reports:
        emit(f"· {report}")

    atomic_write(ENV_PATH, new_env, mode=0o600)

    # ---- 回读校验 ----
    final_env = {}
    for line in read_text(ENV_PATH).splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            final_env[m.group(1)] = m.group(2)
    problems = [k for k, v in ENV_CHANGES if final_env.get(k) != v]
    if problems:
        fail(f".env.server 回读校验失败：{problems}")

    section("5. 验证结果")
    final_config = parse_yaml(read_text(CONFIG_PATH), "config.yml")
    ds = yaml_get(final_config, ("stages", STAGE) + DASHSCOPE_KEY_PATH)
    tv = yaml_get(final_config, ("stages", STAGE) + TAVILY_KEY_PATH)
    emit(f"DashScope API Key = {'SET' if ds else 'NOT SET'}")
    emit(f"Tavily API Key = {'SET' if tv else 'NOT SET'}")
    emit(f"APP_ENV = {final_env.get('APP_ENV')}")
    emit(f"ALLOW_LIVE_EXTERNAL_APIS = {final_env.get('ALLOW_LIVE_EXTERNAL_APIS')}")
    emit("")
    emit("（为跑通 E2E 一并切换的 provider 开关）")
    emit(f"LLM_PROVIDER = {final_env.get('LLM_PROVIDER')}")
    emit(f"SEARCH_PROVIDER = {final_env.get('SEARCH_PROVIDER')}")

    section("完成")
    emit("· 未启动任何服务，未调用任何 API。")
    emit("· 备份：config.yml.bak / .env.server.bak")
    emit("· 下一步（由你决定何时执行）：bash scripts/autodl/start_all.sh")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        emit("\n已取消，未做任何写入。")
        sys.exit(130)
