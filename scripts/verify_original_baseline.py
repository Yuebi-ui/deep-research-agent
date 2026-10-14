#!/usr/bin/env python3
"""Verify frozen original tests archive and unchanged historical benchmarks/results.

Offline stdlib only. Does not execute tests, rewrite experiments, or compare V3
active tests to old serial-only expectations. Exit nonzero if the frozen
content is unexpectedly changed, added, missing, or corrupted.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "historical/original_sha256_manifest.json"
ARCHIVE = ROOT / "historical/original-tests.zip"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def check_files(root: Path, expected: dict[str, str], group: str) -> list[str]:
    errors: list[str] = []
    actual = {str(p.relative_to(root).as_posix()): digest(p.read_bytes())
              for p in (root / group).rglob("*") if p.is_file()}
    for name in sorted(set(expected) - set(actual)):
        errors.append(f"missing {name}")
    for name in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected {name}")
    for name in sorted(set(actual) & set(expected)):
        if actual[name] != expected[name]:
            errors.append(f"changed {name}")
    return errors


def check(root: Path = ROOT, *, expected_manifest: Path = MANIFEST,
          archive: Path = ARCHIVE) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads(expected_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot load baseline manifest: {exc}"]
    if manifest.get("schema") != "deep-research-agent-historical-baseline-v1":
        return ["unrecognized manifest schema"]
    old = manifest["original"]
    for group in ("benchmarks", "results"):
        errors.extend(check_files(root, old[group], group))
    try:
        if digest(archive.read_bytes()) != manifest["archived_tests_zip_sha256"]:
            errors.append("original tests archive SHA-256 mismatch")
        with zipfile.ZipFile(archive) as z:
            corrupt = z.testzip()
            if corrupt:
                errors.append(f"corrupt archived test: {corrupt}")
            actual_names = set(z.namelist())
            expected_names = set(old["tests"])
            for name in sorted(expected_names - actual_names):
                errors.append(f"archived original test missing: {name}")
            for name in sorted(actual_names - expected_names):
                errors.append(f"unexpected archived test: {name}")
            for name in sorted(expected_names & actual_names):
                if digest(z.read(name)) != old["tests"][name]:
                    errors.append(f"changed archived original test: {name}")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        errors.append(f"cannot open archived tests: {exc}")
    return errors


def main() -> int:
    problems = check()
    if problems:
        print("Historical baseline: FAIL")
        for item in problems:
            print(" -", item)
        return 1
    print("Historical baseline: PASS (original tests, benchmarks and results)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
