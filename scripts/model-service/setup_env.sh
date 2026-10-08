#!/usr/bin/env bash
# 创建/更新独立的 vLLM Model Service 环境（与 app 的 .venv 完全隔离）。
#
#     bash scripts/model-service/setup_env.sh
#
# ## 为什么固定 vllm==0.19.1
#
# 本机（AutoDL 4090D 实例）NVIDIA 驱动为 570.124.04，只能运行 CUDA 12.x
# 的 torch wheel。实测 PyPI 元数据：
#
#   vllm <= 0.19.1  → torch==2.10.0  → nvidia-*-cu12（CUDA 12.8）  ✅ 驱动兼容
#   vllm >= 0.20.x  → torch==2.11.0+ → nvidia-*-cu13（CUDA 13）   ✗ 需要驱动 ≥580
#
# 因此选 PyPI 上最新的 CUDA 12.8 兼容版 0.19.1。
# 若 GPU 开启后驱动升级到 580+，可再用 VLLM_VERSION 覆盖安装更新版本。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/model-service/_common.sh
. ./_common.sh

VLLM_VERSION="${VLLM_VERSION:-0.19.1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

section "1. 虚拟环境"
if [ -x "${VLLM_VENV}/bin/pip" ]; then
  ok "已存在：${VLLM_VENV}（复用）"
else
  info "创建 ${VLLM_VENV} …"
  "${PYTHON_BIN}" -m venv "${VLLM_VENV}" || die "venv 创建失败（需要 Python >= 3.10）"
  ok "已创建：${VLLM_VENV}"
fi

section "2. 安装 vllm==${VLLM_VERSION}"
info "首次安装需要下载约 5 GB 依赖（torch 2.10.0 CUDA 12.8 栈），耐心等待…"
"${VLLM_VENV}/bin/pip" install --no-input "vllm==${VLLM_VERSION}" || die "vllm 安装失败"

# 下载加速（可选，用于将来从 HF 镜像拉模型）
"${VLLM_VENV}/bin/pip" install --no-input hf_transfer >/dev/null 2>&1 || true

section "3. 校验"
"${VLLM_VENV}/bin/python" -c "
import vllm
print('vllm:', vllm.__version__)
import torch
print('torch:', torch.__version__, '| CUDA(build):', torch.version.cuda)
" || die "导入校验失败"

echo
ok "Model Service 环境就绪：${VLLM_VENV}"
info "注意：torch 仅确认到构建版本（CUDA 12.8）。真正的 GPU/CUDA 运行时"
info "      验证属于 GPU 阶段（PENDING_GPU），见 scripts/model-service/README.md"
