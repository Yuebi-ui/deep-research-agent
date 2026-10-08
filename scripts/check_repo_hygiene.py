#!/usr/bin/env python3
"""Validate the GitHub-facing repository contract without third-party packages."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md",
    ".gitignore",
    ".env.example",
    "pyproject.toml",
    "architecture/README.md",
    "architecture/MEMORY.md",
    "architecture/CODE_MAP.md",
    "docs/PROJECT_STATUS.md",
    "docs/DESIGN_DECISIONS.md",
    "docs/TESTING.md",
    "docs/ROADMAP.md",
    "examples/demo_run.md",
    "deep_research/memory/runtime.py",
    "backend/runtime/memory_outbox.py",
]

FORBIDDEN_EXACT = [
    ".env",
    "config.yml",
    "data/tasks.db",
    "data/research_episodes.sqlite3",
]

FORBIDDEN_ROOT_GLOBS = [
    "*_CHANGESET.patch",
    "*_MANIFEST.json",
    "*_TEST_RESULTS.txt",
    "README_MEMORY*.md",
    "README_PHASE*.md",
]

PRIMARY_MARKDOWN = [
    Path("README.md"),
    Path("CONTRIBUTING.md"),
    Path("SECURITY.md"),
    *sorted(Path("architecture").glob("*.md")),
    *sorted(p for p in Path("docs").glob("*.md") if p.name not in {"E2E_EVIDENCE.md"}),
    *sorted(Path("examples").glob("*.md")),
]

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _clean_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    # Markdown link titles are not used by the primary docs. Keep the parser
    # intentionally narrow so a weird target fails loudly instead of guessing.
    target = target.split("#", 1)[0]
    return target


def check() -> list[str]:
    problems: list[str] = []

    for relative in REQUIRED:
        if not (ROOT / relative).exists():
            problems.append(f"missing required path: {relative}")

    for relative in FORBIDDEN_EXACT:
        if (ROOT / relative).exists():
            problems.append(f"local/private state must not be committed: {relative}")

    for pattern in FORBIDDEN_ROOT_GLOBS:
        for path in ROOT.glob(pattern):
            problems.append(f"internal release artifact should not be in the public root: {path.name}")

    for pattern in ("memory3-offline-*", "offline-memory-*"):
        for path in ROOT.glob(pattern):
            if path.is_dir():
                problems.append(f"test-generated sandbox must not be committed: {path.name}/")

    for relative in PRIMARY_MARKDOWN:
        source = ROOT / relative
        if not source.exists():
            continue
        text = source.read_text("utf-8")
        for raw in LINK_RE.findall(text):
            target = _clean_target(raw)
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (source.parent / target).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                problems.append(f"{relative}: local link escapes repository: {raw}")
                continue
            if not resolved.exists():
                problems.append(f"{relative}: broken local link: {raw}")

    # Compose must not advertise a build context that is absent from the repo.
    compose = ROOT / "docker-compose.yml"
    if compose.exists():
        for line in compose.read_text("utf-8").splitlines():
            match = re.match(r"\s*build:\s*([^#]+?)\s*$", line)
            if not match:
                continue
            raw = match.group(1).strip().strip('"\'')
            context = (ROOT / raw).resolve()
            if not context.exists():
                problems.append(f"docker-compose.yml: missing build context: {raw}")

    return problems


def main() -> int:
    problems = check()
    if problems:
        print("Repository contract: FAIL")
        for item in problems:
            print(f" - {item}")
        return 1
    print("Repository contract: PASS")
    print(f"Checked {len(REQUIRED)} required entry points and primary Markdown links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
