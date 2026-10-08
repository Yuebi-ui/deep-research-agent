# AutoDL 部署备忘（Phase G 未实际部署）

> 对应 `PHASE_G_WORKER_RUNTIME_EXECUTION_PLAN.md` §33。
>
> **本文件只记录将来部署 24GB GPU 机器所需的事实，不包含部署步骤，
> 也没有在本 Phase 实际执行任何部署。**
>
> 按 §3 的约束：本轮禁止 GPU / CUDA / vLLM、禁止 AutoDL 正式部署、
> 禁止下载模型。

---

## 1. Processes

与 `docs/server-v1-readiness.md` §2 相同，额外增加一个 Model Service：

```text
api        FastAPI（命令 / 查询 / 事件投影）
worker     python -m backend.worker（唯一 LangGraph 执行者）
redis      redis-stack-server
frontend   Next.js
model      本机推理服务（OpenAI-compatible HTTP）—— **新增**
```

Model Service **不在本轮实现**，只保留接口位置。

---

## 2. Environment

在 Server V1 的基础上，Model Service 相关只需要改 provider 的指向，
**不改任何 Agent 代码**：

```dotenv
# 24GB 机器
LLM_BASE_URL=http://model:8000/v1
LLM_MODEL=<本机模型名>
LLM_PROVIDER=auto
ALLOW_LIVE_EXTERNAL_APIS=true

# 若仍需云端作为回退，保留原 base_url 与 api_key 即可
```

---

## 3. Redis Stack Requirement

**不变，且是硬性的**：

```text
redis/redis-stack-server:latest
```

`langgraph-checkpoint-redis` 需要 RediSearch / RedisJSON。
换机器不会改变这个依赖。

---

## 4. Data / Log Directories

```text
/app/data          挂载为持久卷 —— 内含 tasks.db（业务事实源）
/app/data/chroma   向量记忆（如启用）
/app/logs          日志（生产建议直接输出 stdout/stderr 由平台收集）
/models            模型权重挂载点 —— **不进业务镜像**
```

---

## 5. Checkpoint Backend

```text
CHECKPOINTER_BACKEND=redis
```

跨进程恢复（worker 崩溃后由新 worker 接管）依赖持久化 checkpointer。
用 `memory` 会让 Phase F 已验证的恢复能力失效——
worker 启动时会**直接拒绝** memory 后端。

---

## 6. Startup Order

```text
1. Redis Stack（healthcheck 通过）
2. alembic upgrade head
3. Model Service（确认 /v1/models 可达）
4. Worker
5. API
6. Frontend
```

Model Service 先于 Worker 启动，避免首次推理请求失败。

---

## 7. Future Model Endpoint

架构上必须保持的边界：

```text
Worker
  │  OpenAI-compatible HTTP
  ▼
Model Service（独立进程 / 独立容器）
  │
  ▼
GPU
```

**禁止**：

```text
Worker / API 直接 torch.load()
把 20GB 权重打进 deep-research-api 镜像
Agent 代码按"是否 GPU 机器"分支
```

理由：

* 换模型不需要重新 build 业务镜像
* 镜像体积与 push/pull 时间可控
* 业务代码与 CUDA 环境解耦
* Model Service 可独立重启而不影响执行中的任务（有 checkpoint 兜底）

---

## 8. 部署前必须先验证的 Host 条件

```bash
nvidia-smi                       # 驱动可用
docker info | grep -i runtime    # NVIDIA Container Runtime 已注册
```

这部分属于**目标服务器的部署准备**，不属于本项目业务测试。
本轮未执行。

---

## 9. 本 Phase 明确未做的事

```text
[ ] 下载任何模型权重
[ ] 安装 CUDA / vLLM / PyTorch GPU 版
[ ] 实现 Model Service
[ ] 在 AutoDL 上部署
[ ] 调优显存 / 量化 / 批处理
[ ] 跨架构镜像构建（开发机为 AMD64，与目标机同架构，无需 buildx）
```

留待「把 Model Service 作为独立 feature 引入」时一并处理，
届时需要同步增加对应 integration tests 与 CI service。
