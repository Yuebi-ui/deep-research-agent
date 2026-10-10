#!/usr/bin/env python
"""Phase 3C P0：Structured Memory embedding schema 显式迁移。

    .venv/bin/python scripts/migrate_memory_schema.py            # dry-run（默认，只读）
    .venv/bin/python scripts/migrate_memory_schema.py --apply    # 执行迁移

背景：Phase 3C 之前，structured memory 的 document 由 Chroma 默认 EF
（ONNX MiniLM，384 维）隐式编码，而 query 走 DashScope text-embedding-v4
（1024 维）——两个 collection 的向量空间不一致（correctness bug）。
修复后（统一显式 embedding + identity marker）旧 collection 会被显式拒绝，
必须经过本迁移重编码。

迁移保真：ids / documents / metadatas 原样保留，只重算向量。
--apply 前默认整目录备份到 ``<persist_dir>.backup-<timestamp>``。

安全阀：
- 默认以当前配置的 live embedding（DashScope）迁移；若当前环境解析为
  fake（离线/测试），--apply 会被拒绝（--allow-fake 仅用于测试环境）。
- 退出码：0=成功（dry-run 也算）；2=有 collection 需要迁移但未 --apply；
  3=环境/参数不安全；4=迁移或校验失败。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _write_report(report_dict: dict, out_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = out_dir / stamp
    target.mkdir(parents=True, exist_ok=True)
    report_dict["generated_at"] = datetime.now().isoformat(timespec="seconds")
    path = target / "report.json"
    path.write_text(json.dumps(report_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="执行迁移（默认 dry-run）")
    parser.add_argument("--persist-dir", default=None, help="Chroma 目录（默认 data/chroma）")
    parser.add_argument("--allow-fake", action="store_true", help="允许在 fake embedding 模式下迁移（仅测试）")
    parser.add_argument("--no-backup", action="store_true", help="跳过迁移前整目录备份（不推荐）")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts" / "memory_migration")
    args = parser.parse_args(argv)

    from deep_research.memory.embeddings import EmbeddingClient
    from deep_research.memory.migration import run_migration
    from deep_research.settings import get_engine_settings

    persist_dir = Path(args.persist_dir) if args.persist_dir else (
        get_engine_settings().resolved_memory_data_dir / "chroma"
    )

    embedder = EmbeddingClient()
    identity = embedder.identity
    print(f"[migrate] persist_dir = {persist_dir}")
    print(f"[migrate] target identity = {identity.describe()}")
    print(f"[migrate] mode = {'APPLY' if args.apply else 'DRY-RUN'}")

    if embedder.is_fake and args.apply and not args.allow_fake:
        print(
            "[FAIL] 当前环境解析为 fake embedding（APP_ENV=test 或 "
            "ALLOW_LIVE_EXTERNAL_APIS=false）——迁移会把真实数据接管到伪向量空间。"
            "请用 live 环境运行，或显式 --allow-fake（仅测试）。",
            file=sys.stderr,
        )
        return 3

    if not persist_dir.exists():
        print(f"[FAIL] Chroma 目录不存在: {persist_dir}", file=sys.stderr)
        return 3

    try:
        report = run_migration(str(persist_dir), embedder, apply=args.apply)
    except Exception as exc:  # noqa: BLE001  # 迁移失败必须显式可见
        print(f"[FAIL] 迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    migrations = [p for p in report.plans if p.action == "migrate"]
    for plan in report.plans:
        mark = "MIGRATE" if plan.action == "migrate" else "ok"
        detail = f" ({plan.reason})" if plan.action == "migrate" else ""
        print(f"  [{mark:>7}] {plan.name:<24} docs={plan.count}{detail}")

    if not args.apply:
        if migrations:
            print(f"[dry-run] {len(migrations)} 个 collection 需要迁移；用 --apply 执行。")
            return 2
        print("[dry-run] 全部 collection 已兼容，无需迁移。")
        return 0

    # 备份（默认开启；仅在有实际迁移时才有意义）
    if migrations and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_dir = persist_dir.parent / f"{persist_dir.name}.backup-{stamp}"
        shutil.copytree(persist_dir, backup_dir)
        report_dict_extra = {"backup_dir": str(backup_dir)}
        print(f"[migrate] 已备份 → {backup_dir}")
    else:
        report_dict_extra = {}

    report_dict = report.to_dict()
    report_dict.update(report_dict_extra)
    path = _write_report(report_dict, args.out_dir)
    print(f"[migrate] 报告 → {path}")

    failed = [r for r in report.results if r.get("action") == "migrated" and not r.get("verified")]
    if failed:
        print(f"[FAIL] 校验失败: {failed}", file=sys.stderr)
        return 4
    migrated = [r for r in report.results if r.get("action") == "migrated"]
    print(f"[OK] 迁移完成：{len(migrated)} 个 collection 重编码并校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
