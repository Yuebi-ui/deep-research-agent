"""统一 Embedding 身份与客户端（Phase 3C P0）。

**背景（correctness bug 修复）**：修复前 `StructuredMemoryStore` 写入时不给
Chroma 传 embeddings，Chroma 用默认 EF（ONNX all-MiniLM-L6-v2，384 维）隐式
编码；而检索时走 DashScope text-embedding-v4（1024 维）。document 与 query
来自完全不同的 embedding space，检索在维度上就直接失败。

**本模块是项目内唯一的 embedding 生成入口**：`VectorMemoryStore` 与
`StructuredMemoryStore` 都通过 :class:`EmbeddingClient` 取向量，写入与检索
共用同一个 identity（provider / model / dimension / schema_version）。

**为什么还要 identity marker**：Chroma 1.5.9 实测中 ``embedding_function=None``
并不会禁用默认 EF——不显式传 embeddings 时仍会被静默编码成 384 维。因此防线
是双重的：

1. 代码路径**永远显式传 embeddings**（见 ``schema_guard.ManagedCollection``）；
2. collection 创建时写入 identity marker，打开时校验；旧 collection（无
   marker 或 marker 不匹配）显式拒绝，绝不静默查询。

离线/测试模式使用确定性伪向量（:func:`fake_embedding`），provider 标记为
``fake``，与真实 DashScope collection 互不兼容——防止测试向量污染真实数据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from openai import OpenAI

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings
from deep_research.utils import load_config

logger = dr_logging.get_logger(__name__)

# 当前 embedding schema 版本。任何导致向量语义不兼容的变更都要 +1。
EMBEDDING_SCHEMA_VERSION = 1

# 值域约定：与历史实现保持一致（DashScope text-embedding-v4 默认 1024 维）
_EMBEDDING_DIMS = 1024
_EMBEDDING_MODEL = "text-embedding-v4"
_FAKE_MODEL = "dr-fake-embedding-v1"

LIVE_PROVIDER = "dashscope"
FAKE_PROVIDER = "fake"

# collection metadata 中的 marker 键（Chroma metadata 只接受标量）
MARKER_PROVIDER = "dr_embedding_provider"
MARKER_MODEL = "dr_embedding_model"
MARKER_DIMENSION = "dr_embedding_dimension"
MARKER_SCHEMA_VERSION = "dr_embedding_schema_version"

# DashScope text-embedding-v4 单次请求 input 数量上限（实测 >10 返回 400
# InvalidParameter: "batch size is invalid, it should not be larger than 10"）。
# EmbeddingClient.embed 负责自动分批，调用方无需关心批量大小。
MAX_BATCH_SIZE = 10


def fake_embedding(text: str, dims: int = _EMBEDDING_DIMS) -> list[float]:
    """由文本哈希生成的确定性伪向量，取值域 [-1, 1]。

    使用 sha256 而非内置 ``hash()``——后者带进程级随机盐，同一文本在
    不同进程会得到不同向量，导致测试不可复现。
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(digest[i % len(digest)] / 127.5) - 1.0 for i in range(dims)]


@dataclass(frozen=True)
class EmbeddingIdentity:
    """一个 embedding space 的完整身份：四元组 + 输出维度。

    ``provider`` 区分 live（dashscope）与 fake（离线确定性伪向量）——
    两种空间的向量**不可混用**，否则维度可能相同但语义完全不同。
    """

    provider: str
    model: str
    dimension: int
    schema_version: int = EMBEDDING_SCHEMA_VERSION

    def as_markers(self) -> dict[str, str | int]:
        return {
            MARKER_PROVIDER: self.provider,
            MARKER_MODEL: self.model,
            MARKER_DIMENSION: self.dimension,
            MARKER_SCHEMA_VERSION: self.schema_version,
        }

    @classmethod
    def from_markers(cls, metadata: dict | None) -> "EmbeddingIdentity | None":
        """从 collection metadata 还原身份；缺任一 marker 返回 None（= legacy）。"""
        meta = metadata or {}
        try:
            provider = meta[MARKER_PROVIDER]
            model = meta[MARKER_MODEL]
            dimension = int(meta[MARKER_DIMENSION])
            schema_version = int(meta[MARKER_SCHEMA_VERSION])
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            provider=str(provider),
            model=str(model),
            dimension=dimension,
            schema_version=schema_version,
        )

    def describe(self) -> str:
        return (
            f"{self.provider}/{self.model} dim={self.dimension} "
            f"schema=v{self.schema_version}"
        )


class EmbeddingClient:
    """统一 embedding 客户端：DashScope（live）或确定性伪向量（fake）。"""

    def __init__(self, *, force_fake: bool | None = None) -> None:
        if force_fake is None:
            force_fake = get_engine_settings().use_fake_embeddings
        self._fake = bool(force_fake)
        self._openai: OpenAI | None = None if self._fake else self._build_embedding_client()

    # 保留原 VectorMemoryStore 的构造语义：离线模式不读配置、不建客户端
    def _build_embedding_client(self) -> OpenAI:
        cfg = load_config(stage_name=get_engine_settings().stage)
        api_cfg = cfg.get("cognition", {}).get("openai", {})
        return OpenAI(
            api_key=api_cfg.get("api_key", ""),
            base_url=api_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )

    @property
    def identity(self) -> EmbeddingIdentity:
        if self._fake:
            return EmbeddingIdentity(
                provider=FAKE_PROVIDER, model=_FAKE_MODEL, dimension=_EMBEDDING_DIMS
            )
        return EmbeddingIdentity(
            provider=LIVE_PROVIDER, model=_EMBEDDING_MODEL, dimension=_EMBEDDING_DIMS
        )

    @property
    def is_fake(self) -> bool:
        return self._fake

    def embed(self, texts: list[str]) -> list[list[float]]:
        """获取文本向量（自动按 provider 上限分批，返回顺序与输入一致）。

        离线模式返回确定性伪向量，不发起网络调用。
        """
        if self._fake:
            return [fake_embedding(text) for text in texts]
        assert self._openai is not None
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            chunk = texts[start : start + MAX_BATCH_SIZE]
            resp = self._openai.embeddings.create(model=_EMBEDDING_MODEL, input=chunk)
            vectors.extend(list(d.embedding) for d in resp.data)
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
