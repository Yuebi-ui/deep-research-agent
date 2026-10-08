#!/usr/bin/env python
"""离线导出 FastAPI OpenAPI schema。

执行计划 §9：Pydantic → OpenAPI → TS types。

刻意**不依赖运行中的服务器** —— 直接从 app 对象取 schema，
因此 CI 里也能稳定生成（无需先起 uvicorn，也就没有端口/时序问题）。

用法：

    python scripts/export_openapi.py                    # 写到 openapi.json
    python scripts/export_openapi.py --out path.json
    python scripts/export_openapi.py --check            # 校验已存在的文件是否最新
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 导入 backend.main 需要这些前置条件（否则 lifespan/settings 会报错）
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_LIVE_EXTERNAL_APIS", "false")

DEFAULT_OUT = REPO_ROOT / "openapi.json"


def build_schema() -> dict:
    """构造 OpenAPI schema。

    只取 `app.openapi()`，不触发 lifespan —— 不连 Redis、不建 DB 连接。
    """
    from backend.main import app

    app.openapi_schema = None  # 避免缓存
    return app.openapi()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线导出 OpenAPI schema")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check", action="store_true",
        help="只校验文件是否为最新（CI 用），不写入",
    )
    args = parser.parse_args(argv)

    schema = build_schema()
    rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.out.exists():
            print(f"[FAIL] {args.out} 不存在；请运行 python scripts/export_openapi.py")
            return 1
        if args.out.read_text(encoding="utf-8") != rendered:
            print(
                f"[FAIL] {args.out} 与当前 schema 不一致。\n"
                "       API 契约已变更但生成物未更新。\n"
                "       请运行: python scripts/export_openapi.py && npm run api:types"
            )
            return 1
        print(f"[OK] {args.out} 是最新的")
        return 0

    args.out.write_text(rendered, encoding="utf-8")
    paths = len(schema.get("paths", {}))
    schemas = len(schema.get("components", {}).get("schemas", {}))
    print(f"已写入 {args.out}（{paths} 个 path，{schemas} 个 schema）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
