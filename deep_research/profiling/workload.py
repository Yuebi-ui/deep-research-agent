"""Phase 4B：claim_verification burst 的**固定 workload manifest**。

目的：让 4/6/8/10/12 各并发档跑**完全相同**的请求集合（same requests、same
order、same generation params），从而 burst wall 的差异只可能来自 admission
window，而不是 workload 抖动。

形状来源（可审计）：

- Phase 4A 三次 E2E（phase4a-p{1,2,3}）中 `claim_verification` 节点内的全部
  60 次本地 `researcher_summarizer` 调用（20 次/run）的 input token 数；
- 合并排序后**等间隔秩取样** 20 个（rank = 0,3,6,…,57）→ 覆盖 1.9k→6.9k，
  且保持真实分布的长尾特征（真实 burst 中约 2/3 的页面在 6.6k 以上）；
- 合计 prompt tokens ≈ 124k，与真实 run 的 cv burst（≈120k）同量级。

内容：**确定性合成网页正文**（seeded 生成 + vLLM /tokenize 精确拟合），
每个请求的正文互不相同（保持与生产一致的"无明显公共前缀"特征：
生产 prefix cache token 命中占比 8–11%，且集中在很短的模板头上）。
不含任何真实网页内容、用户数据或密钥。

设计约束：

- 纯函数：`render_prompt` / `build_manifest` / `manifest_fingerprint` 只依赖入参
  与注入的 counter，可在测试中用假 counter 运行；
- manifest 一旦生成即**冻结**：prompt 原文与 sha1 落盘，后续所有 run 直接复用，
  重建时用 `verify_manifest` 校验指纹。
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from deep_research.prompts.summarize_webpage import SUMMARIZE_PROMPT

# ===== 固定形状（见模块 docstring 的来源说明）=====

CV_BURST_SHAPES: tuple[int, ...] = (
    1903, 2434, 3316, 4294, 5030, 5610, 6024, 6480, 6666, 6739,
    6819, 6823, 6828, 6863, 6877, 6896, 6898, 6901, 6914, 6923,
)

# 生产观测：cv 本地调用 completion tokens 278–520（p50 372）。600 留出头部余量
# 同时限定单个请求的生成上界，保证固定工作量。
DEFAULT_MAX_TOKENS = 600

# 冻结日期：production 用 get_today_str()，manifest 用固定值以保证任意日期
# 重建都得到同一份 prompt（否则 sha1 会随日期变化，"冻结"就失效了）。
FROZEN_DATE = "Mon Oct 6, 2025"

# max_model_len=8192：prompt + max_tokens 必须留有余量
CONTEXT_LIMIT = 8192

_BODY_WORDS: tuple[str, ...] = (
    "report", "market", "growth", "region", "policy", "study", "sample", "method",
    "result", "trend", "sector", "quarter", "budget", "review", "analysis", "figure",
    "measure", "program", "impact", "factor", "signal", "baseline", "estimate", "range",
    "period", "concept", "structure", "network", "process", "system", "model", "index",
    "record", "session", "summary", "context", "volume", "segment", "timeline", "detail",
    "however", "therefore", "meanwhile", "overall", "additionally", "notably", "similarly", "finally",
)


def _body_text(seed: int, min_chars: int) -> str:
    """确定性合成正文：同一 seed 永远得到同一段文本，不同 seed 得到不同文本。"""
    rng = random.Random(seed)
    parts: list[str] = []
    total = 0
    while total < min_chars:
        words = " ".join(rng.choice(_BODY_WORDS) for _ in range(rng.randint(8, 16)))
        sentence = words.capitalize() + "."
        parts.append(sentence)
        total += len(sentence) + 1
    return " ".join(parts)


def _fit_body_to_tokens(seed: int, body_budget_tokens: int, build: Callable[[str], str],
                        counter: Callable[[str], int], target: int) -> tuple[str, int]:
    """生成正文并裁剪到不超过 target tokens —— **只裁正文，模板头尾保持完整**。

    生产路径（context_budget.fit_prompt_for_role）裁的也是网页正文，指令与
    输出格式要求始终保留；裁掉模板尾部的 JSON 格式要求会让输出形状失真。
    """
    body_full = ""
    ratio = 4  # 初始按 4 字符/token 粗估，不足则翻倍重试
    for _ in range(5):
        body_full = _body_text(seed, min_chars=max(200, body_budget_tokens * ratio))
        if counter(build(body_full)) >= target:
            break
        ratio *= 2
    if counter(build(body_full)) <= target:
        return build(body_full), counter(build(body_full))

    lo, hi = 0, len(body_full)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter(build(body_full[:mid])) <= target:
            lo = mid
        else:
            hi = mid - 1
    text = build(body_full[:lo])
    return text, counter(text)


def render_prompt(seed: int, target_tokens: int, counter: Callable[[str], int]) -> tuple[str, int]:
    """生成 <= target_tokens 的确定性 prompt（真实 SUMMARIZE_PROMPT 模板 + 合成正文）。

    返回 (prompt_text, prompt_tokens)。target_tokens 对齐生产 input_tokens
    （即模板头 + 正文 + 模板尾的总和）。
    """
    header = SUMMARIZE_PROMPT.format(webpage_content="", date=FROZEN_DATE)
    header_tokens = counter(header)
    if target_tokens <= header_tokens:
        raise ValueError(
            f"target_tokens={target_tokens} 小于模板自身开销 {header_tokens}，"
            "形状不可复现（请提高目标或改用更短模板）"
        )
    build = lambda body: SUMMARIZE_PROMPT.format(webpage_content=body, date=FROZEN_DATE)  # noqa: E731
    return _fit_body_to_tokens(seed, target_tokens - header_tokens, build, counter, target_tokens)


def _sha1_12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def build_manifest(
    counter: Callable[[str], int],
    *,
    shapes: Sequence[int] = CV_BURST_SHAPES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed_base: int = 20261006,
) -> dict[str, Any]:
    """构造固定 workload manifest（纯函数；counter 由调用方注入）。

    seed 从 manifest 索引派生 → 每个请求正文唯一且可复现；生成后校验
    prompt sha1 互不相同（防止意外重复导致 prefix cache 假命中）。
    """
    requests: list[dict[str, Any]] = []
    for index, target in enumerate(shapes):
        seed = seed_base + index * 7919
        prompt, tokens = render_prompt(seed, int(target), counter)
        requests.append({
            "index": index,
            "seed": seed,
            "target_tokens": int(target),
            "prompt_tokens": int(tokens),
            "prompt_sha1": _sha1_12(prompt),
            "max_tokens": int(max_tokens),
            "prompt": prompt,
        })

    hashes = [r["prompt_sha1"] for r in requests]
    if len(set(hashes)) != len(hashes):
        raise ValueError("manifest 内出现重复 prompt（会引入 prefix cache 假命中）")
    over = [r["index"] for r in requests if r["prompt_tokens"] + max_tokens > CONTEXT_LIMIT]
    if over:
        raise ValueError(f"请求 {over} 的 prompt+max_tokens 超过上下文上限 {CONTEXT_LIMIT}")

    return {
        "schema": "phase4b.workload.v1",
        "source": "phase4a claim_verification researcher_summarizer shapes (60 calls / 3 runs)",
        "date": FROZEN_DATE,
        "max_tokens": int(max_tokens),
        "context_limit": CONTEXT_LIMIT,
        "fingerprint": manifest_fingerprint(requests),
        "requests": requests,
    }


def manifest_fingerprint(requests: Iterable[dict]) -> str:
    """manifest 指纹：index + prompt sha1 + max_tokens 的顺序敏感哈希。"""
    payload = "\n".join(
        f"{r['index']}:{r['prompt_sha1']}:{r['max_tokens']}" for r in requests
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def verify_manifest(manifest: dict) -> list[str]:
    """校验 manifest 自洽性，返回问题列表（空 = OK）。不读取任何外部状态。"""
    problems: list[str] = []
    requests = manifest.get("requests") or []
    if not requests:
        return ["manifest 不含任何请求"]
    if manifest.get("fingerprint") != manifest_fingerprint(requests):
        problems.append("fingerprint 与 requests 不一致（manifest 被改动过）")
    max_tokens = manifest.get("max_tokens")
    for r in requests:
        for key in ("index", "prompt", "prompt_sha1", "prompt_tokens", "max_tokens"):
            if key not in r:
                problems.append(f"请求 {r.get('index')} 缺少字段 {key}")
        if r.get("prompt_sha1") != _sha1_12(r.get("prompt", "")):
            problems.append(f"请求 {r.get('index')} 的 prompt sha1 不匹配")
        if r.get("prompt_tokens", 0) + (max_tokens or 0) > CONTEXT_LIMIT:
            problems.append(f"请求 {r.get('index')} 超出上下文上限")
    return problems


def write_manifest(path: Path, manifest: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
