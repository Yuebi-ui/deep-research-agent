#!/usr/bin/env python3
"""Check a source-only AutoDL repository without optional deleted documents.

Uses only the Python standard library. Check that retained entry points exist,
private runtime files are not accidentally committed, and links in the public
Markdown files resolve inside this repository. Historical documents intentionally
removed from the project are not restored or treated as mandatory.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

# Only keep mandatory executable/configuration entry points in this list.
# Optional explanatory Markdown is checked through links, not by filename.
REQUIRED: tuple[str, ...] = (
    "README.md",
    ".gitignore",
    ".env.example",
    "config.server.example.yml",
    "config.hybrid.example.yml",
    "pyproject.toml",
    "requirements.txt",
    "backend/main.py",
    "backend/worker.py",
    "backend/memory_worker.py",
    "deep_research/agent_builder.py",
    "deep_research/memory/runtime.py",
    "backend/runtime/memory_outbox.py",
    "scripts/autodl/setup.sh",
    "scripts/autodl/start_redis.sh",
    "scripts/autodl/start_all.sh",
    "scripts/model-service/start_vllm.sh",
    "scripts/model-service/healthcheck_vllm.sh",
    "benchmarks/run_offline.py",
    "benchmarks/run_live.py",
    "results/offline/v1/retrieval_summary.json",
    "results/offline/v1/manifest.json",
)

PRIVATE_PATHS: tuple[str, ...] = (
    # These MAY exist in the working tree after a real deployment, but must
    # never be staged/committed. In a ZIP without .git, their presence fails.
    ".env",
    ".env.server",
    "config.yml",
    "data/tasks.db",
    "data/research_episodes.sqlite3",
    "results/offline/v1/retrieval_per_query.jsonl",
    "results/examples/reference_v1/task_runs.synthetic.jsonl",
    "results/examples/reference_v1/fault_observations.synthetic.jsonl",
)

RETIRED_DEPLOYMENT_FILES: tuple[str, ...] = (
    # These files are deliberately absent from the AutoDL-only source package.
    "Dockerfile",
    "docker-compose.yml",
    "config.docker.example.yml",
    ".dockerignore",
)

FORBIDDEN_ROOT_GLOBS: tuple[str, ...] = (
    "*_CHANGESET.patch",
    "*_MANIFEST.json",
    "*_TEST_RESULTS.txt",
    "README_MEMORY*.md",
    "README_PHASE*.md",
)

PUBLIC_DOC_DIRS: tuple[str, ...] = (
    "architecture",
    "docs",
    "examples",
    "benchmarks",
    "results",
    "scripts/model-service",
)

LINK_RE = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)]+)\)")
EXTERNAL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _link_target(raw: str) -> str | None:
    """Return a local relative target, or None for anchors/external links."""
    target = raw.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing != -1:
            target = target[1:closing]
    else:
        # Optional Markdown link title follows the path after whitespace.
        target = target.split(None, 1)[0] if target else ""

    if not target or target.startswith(("#", "//")) or EXTERNAL_SCHEME_RE.match(target):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return target or None


def _public_markdown(root: Path) -> list[Path]:
    """Enumerate actual Markdown files relative to root (never relative to cwd)."""
    pages = set(root.glob("*.md"))
    for directory in PUBLIC_DOC_DIRS:
        base = root / directory
        if base.is_dir():
            pages.update(base.rglob("*.md"))
    return sorted(path for path in pages if path.is_file())


def _git_tracked(root: Path) -> set[str] | None:
    """Return Git index paths, or None when inspecting a ZIP without Git data."""
    if not (root / ".git").exists() or not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None
    return {path for path in result.stdout.decode("utf-8", "replace").split("\0") if path}


def check(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    problems: list[str] = []

    for relative in REQUIRED:
        if not (root / relative).is_file():
            problems.append(f"missing required path: {relative}")

    tracked = _git_tracked(root)
    for relative in PRIVATE_PATHS:
        # Git checkout: ignored local .env.server / config.yml are legitimate.
        # Source archive (no .git): a bundled private file is always an error.
        present_in_delivery = (relative in tracked) if tracked is not None else (root / relative).exists()
        if present_in_delivery:
            problems.append(f"private runtime state must not be committed: {relative}")

    for relative in RETIRED_DEPLOYMENT_FILES:
        if (root / relative).exists():
            problems.append(f"retired Docker Compose file should not be in AutoDL distribution: {relative}")

    for pattern in FORBIDDEN_ROOT_GLOBS:
        for path in sorted(root.glob(pattern)):
            problems.append(f"internal release artifact should not be in repository root: {path.name}")

    for pattern in ("memory3-offline-*", "offline-memory-*"):
        for path in sorted(root.glob(pattern)):
            if path.is_dir():
                problems.append(f"test-generated sandbox must not be committed: {path.name}/")

    for source in _public_markdown(root):
        relative = source.relative_to(root).as_posix()
        for raw in LINK_RE.findall(source.read_text(encoding="utf-8")):
            target = _link_target(raw)
            if target is None:
                continue
            resolved = (source.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                problems.append(f"{relative}: local link escapes repository: {raw}")
                continue
            if not resolved.exists():
                problems.append(f"{relative}: broken local link: {raw}")

    return problems


def main() -> int:
    problems = check()
    if problems:
        print("Repository contract: FAIL")
        for item in problems:
            print(f" - {item}")
        return 1

    print("Repository contract: PASS")
    print(f"Checked {len(REQUIRED)} required entry points and public Markdown links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
