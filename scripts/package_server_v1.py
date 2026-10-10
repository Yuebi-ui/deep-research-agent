#!/usr/bin/env python
"""生成 Server V1 部署包。

    python scripts/package_server_v1.py            # 生成 tar.gz
    python scripts/package_server_v1.py --dry-run  # 只列清单，不打包
    python scripts/package_server_v1.py --list     # 同上（别名）

设计原则（**allowlist 优先**）：

本脚本用**显式白名单**决定打包什么，而不是"排除一堆已知坏东西"。
白名单之外的一律不进包 —— 这样将来仓库里新增了敏感文件（真实的
`config.yml`、新的 `.env`、模型权重、内部资料），默认结果是**不打包**，
而不是"不小心打进去了"。

打包前后各有一次安全校验：

```text
打包前  secret 扫描（API Key / Token 形态）
打包后  再扫一次压缩包内容，确认没有漏网
```

任何一步失败都会中止，不会产出带 secret 的包。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_NAME = "deep-research-server-v1.tar.gz"
ARCHIVE_PREFIX = "deep-research-server-v1"

# ===== allowlist：只有这些会进包 =====

#: 目录级白名单 —— 整棵子树都打（但会应用下面的 EXCLUDE_PATTERNS）
INCLUDE_DIRS: tuple[str, ...] = (
    "deep_research",
    "backend",
    "migrations",
    "scripts",
    "tests",
    "docs",
    "architecture",
    "examples",
    ".github",
)

#: 文件级白名单（仓库根）
INCLUDE_FILES: tuple[str, ...] = (
    "alembic.ini",
    "pytest.ini",
    "ruff.toml",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "config.server.example.yml",
    "config.hybrid.example.yml",
    ".env.example",
    ".gitignore",
    ".gitattributes",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
)

#: 即使在白名单目录内也要排除的路径片段（大小写不敏感）
EXCLUDE_PATTERNS: tuple[str, ...] = (
    # 依赖与构建产物
    "node_modules", ".next", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "*.pyc", "*.pyo",
    "dist", "build", "*.egg-info", "tsconfig.tsbuildinfo",
    # 运行时数据
    "data", "logs", "results", "*.db", "*.sqlite", "*.sqlite3",
    # 秘密
    "config.yml", ".env", ".env.local", ".env.test",
    # 本地 UI 覆盖（如果未来挂载独立 UI 客户端）
    ".env.development.local", ".env.production.local",
    # 本地归档 / 日志
    "*.zip", "*.tar.gz", "*.log",
)

# ===== secret 扫描 =====

#: 形如真实凭据的字符串（不含明显的占位符）
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DashScope/OpenAI 风格 sk-", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("Tavily tvly-", re.compile(r"\btvly-[A-Za-z0-9]{20,}")),
    ("LangSmith lsv2_", re.compile(r"\blsv2_[A-Za-z0-9_]{20,}")),
    ("通用长 api_key 赋值", re.compile(r"""(?i)api[_-]?key["']?\s*[:=]\s*["']([A-Za-z0-9\-_]{28,})["']""")),
)

#: 出现这些词就认为是占位符，不算 secret
PLACEHOLDER_HINTS = (
    "your-", "your_", "xxx", "placeholder", "example", "changeme",
    "<", ">", "${", "{{", "dummy", "fake", "test-key",
)

#: 这些文件不参与 secret 扫描（本脚本自身、示例文件、二进制）
SKIP_SCAN_SUFFIXES = (".png", ".ico", ".svg", ".jpg", ".jpeg", ".woff", ".woff2")
SKIP_SCAN_NAMES = {ARCHIVE_NAME, "package_server_v1.py"}


def is_excluded(rel_path: Path) -> bool:
    """路径是否命中排除规则。"""
    parts_lower = [p.lower() for p in rel_path.parts]
    name_lower = rel_path.name.lower()

    for pattern in EXCLUDE_PATTERNS:
        pat = pattern.lower()
        if pat.startswith("*."):
            if name_lower.endswith(pat[1:]):
                return True
        elif pat in parts_lower or name_lower == pat:
            return True
    return False


def collect_files() -> tuple[list[Path], list[tuple[str, str]]]:
    """按 allowlist 收集文件。

    返回 (要打包的相对路径列表, 被排除的 (路径, 原因) 列表)。
    """
    included: list[Path] = []
    skipped: list[tuple[str, str]] = []

    for dir_name in INCLUDE_DIRS:
        base = REPO_ROOT / dir_name
        if not base.is_dir():
            skipped.append((dir_name, "目录不存在"))
            continue

        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT)
            if is_excluded(rel):
                continue
            included.append(rel)

    for file_name in INCLUDE_FILES:
        path = REPO_ROOT / file_name
        rel = Path(file_name)
        if not path.is_file():
            skipped.append((file_name, "文件不存在"))
            continue
        if is_excluded(rel):
            continue
        included.append(rel)

    return sorted(set(included)), skipped


def scan_text(text: str) -> list[str]:
    """返回命中的 secret 描述列表（空表示干净）。"""
    hits: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            fragment = match.group(0)
            lowered = fragment.lower()
            if any(hint in lowered for hint in PLACEHOLDER_HINTS):
                continue
            hits.append(f"{label}: {fragment[:12]}…")
    return hits


def scan_files(rel_paths: list[Path]) -> list[tuple[str, str]]:
    """扫描待打包文件。返回 (相对路径, 命中描述) 列表。"""
    findings: list[tuple[str, str]] = []

    for rel in rel_paths:
        if rel.name in SKIP_SCAN_NAMES:
            continue
        if rel.suffix.lower() in SKIP_SCAN_SUFFIXES:
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for hit in scan_text(text):
            findings.append((str(rel), hit))

    return findings


def scan_archive(archive_path: Path) -> list[tuple[str, str]]:
    """打包后再扫一次压缩包内容（防御性复查）。"""
    findings: list[tuple[str, str]] = []

    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if Path(member.name).suffix.lower() in SKIP_SCAN_SUFFIXES:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            text = io.TextIOWrapper(extracted, encoding="utf-8", errors="ignore").read()
            for hit in scan_text(text):
                findings.append((member.name, hit))

    return findings


def build_archive(rel_paths: list[Path], out_path: Path) -> None:
    with tarfile.open(out_path, "w:gz") as tar:
        for rel in rel_paths:
            tar.add(REPO_ROOT / rel, arcname=str(Path(ARCHIVE_PREFIX) / rel))


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} TB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Server V1 部署包")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / ARCHIVE_NAME)
    parser.add_argument("--dry-run", "--list", dest="dry_run", action="store_true",
                        help="只列出会打包的文件，不生成压缩包")
    args = parser.parse_args(argv)

    rel_paths, skipped = collect_files()

    print("=== 打包清单（allowlist 模式）===")
    print(f"  文件数: {len(rel_paths)}")
    by_top: dict[str, int] = {}
    for rel in rel_paths:
        by_top[rel.parts[0]] = by_top.get(rel.parts[0], 0) + 1
    for name, count in sorted(by_top.items()):
        print(f"    {name:28s} {count:4d}")
    if skipped:
        print(f"  （白名单中不存在的条目: {[s[0] for s in skipped]}）")

    # ---- 打包前 secret 扫描 ----
    print()
    print("=== Secret 扫描（打包前）===")
    findings = scan_files(rel_paths)
    if findings:
        print("  ✗ 发现疑似 secret，已中止：")
        for path, hit in findings:
            print(f"    {path}: {hit}")
        return 1
    print("  ✓ 未发现疑似 secret")

    if args.dry_run:
        print()
        print("（--dry-run：未生成压缩包）")
        return 0

    # ---- 生成压缩包 ----
    out_path = args.out
    if out_path.exists():
        out_path.unlink()
    build_archive(rel_paths, out_path)

    size = out_path.stat().st_size
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()[:16]

    # ---- 打包后复查 ----
    print()
    print("=== Secret 扫描（打包后复查）===")
    post = scan_archive(out_path)
    if post:
        print("  ✗ 压缩包内发现疑似 secret，已删除压缩包：")
        for path, hit in post:
            print(f"    {path}: {hit}")
        out_path.unlink()
        return 1
    print("  ✓ 压缩包内容干净")

    print()
    print("=== 完成 ===")
    print(f"  文件: {out_path}")
    print(f"  大小: {_human(size)}")
    print(f"  SHA256(前16位): {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
