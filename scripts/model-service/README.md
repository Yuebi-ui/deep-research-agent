# AutoDL 服务器 GPU vLLM（Local Model Service）

独立于应用（FastAPI / Worker / LangGraph）的服务器端 GPU 推理服务，对 Worker
暴露 **OpenAI-compatible HTTP** 接口：

```text
Worker / LangGraph
      │  OpenAI-compatible HTTP（http://127.0.0.1:8001/v1）
      ▼
AutoDL Server vLLM（本目录脚本管理）
      │  vLLM
      ▼
Qwen/Qwen3-30B-A3B-GPTQ-Int4（本地权重）
      │
      ▼
RTX 4090D 24GB
```

应用侧只通过 `config.yml` 的 `roles` 选择 provider（`openai_local`），
Agent 代码对 Local / Cloud 无感知。

## 目录与文件

```text
脚本（仓库内）                       运行数据（数据盘 /root/autodl-tmp）
scripts/model-service/
├── _common.sh          共用配置      ├── models/Qwen3-30B-A3B-GPTQ-Int4/
├── setup_env.sh        建环境        ├── huggingface/        （HF_HOME）
├── start_vllm.sh       启动          ├── vllm-env/           （独立 venv）
├── stop_vllm.sh        停止          └── logs/vllm/
├── restart_vllm.sh     重启              ├── serve.log       （vLLM 日志）
├── healthcheck_vllm.sh 健康检查          └── vllm.pid        （pidfile）
└── logs_vllm.sh        看日志
```

所有路径/参数可用环境变量覆盖（`MODEL_SERVICE_ROOT`、`VLLM_PORT`、
`VLLM_MAX_MODEL_LEN`、`VLLM_GPU_MEMORY_UTILIZATION` 等，见 `_common.sh`）。

## 常用命令

```bash
bash scripts/model-service/start_vllm.sh          # 启动（需 GPU）
bash scripts/model-service/healthcheck_vllm.sh    # 进程 + /v1/models
bash scripts/model-service/healthcheck_vllm.sh --probe   # 追加最小推理探针
bash scripts/model-service/logs_vllm.sh -f        # 实时日志
bash scripts/model-service/restart_vllm.sh
bash scripts/model-service/stop_vllm.sh
```

## GPU 阶段（RTX 4090D 开启后）执行顺序

1. `bash scripts/model-service/start_vllm.sh`
   —— 首次加载 GPTQ 权重并编译 kernel，耐心等 `/v1/models` 就绪。
2. `bash scripts/model-service/healthcheck_vllm.sh --probe`
   —— 确认能在 8001 上完成一次真实推理。
3. 启用 Hybrid 配置（仅配置改动，无代码改动）：
   ```bash
   cp config.hybrid.example.yml config.yml     # 然后填入两个真实 key
   # .env.server: APP_ENV=development / ALLOW_LIVE_EXTERNAL_APIS=true
   ```
4. 重启应用（Redis 可不动）：
   ```bash
   bash scripts/autodl/stop.sh && bash scripts/autodl/start_all.sh
   ```
5. 跑一次真实 Deep Research E2E（人工建任务），观察 Worker 日志里
   `openai_local` 相关请求与云端角色的分流。

### 全 Cloud 回滚

Local Service 出问题时，把 `config.yml` 中 4 个 LOCAL 角色改回
`openai` / `deepseek-v4-*`（`config.hybrid.example.yml` 末尾有现成片段），
重启应用即可；不需要改代码、不需要动 vLLM。

## PENDING_GPU（必须等 GPU 才能确认的事项）

- vLLM 0.19.1 / torch 2.10.0(cu128) 与驱动 570.124.04 的**运行时**兼容性
- `--tool-call-parser hermes` 是否适配 Qwen3-30B-A3B 的工具调用输出
  （`research_agent` / `supervisor` 使用 `bind_tools`）。
- `chat_template_kwargs.enable_thinking=false` 是否被该 vLLM 版本接受
  （不接受则从 `config.yml` 的 `extra_body` 中删除后复测）。
- `max_model_len=8192` / `gpu_memory_utilization=0.85` 的实际表现
  （长工具输出可能触及上下文上限）。
- 显存占用、首 token 延迟、吞吐量（本轮不做 benchmark）。

以上全部属于 **PENDING_GPU**，不是失败。

## 注意

- 模型权重与 vllm-env 都在**数据盘**，不在系统盘，也不进 Git。
- 本服务只监听 `127.0.0.1:8001`，不对外暴露。
- `APP_ENV=test`（Fake 模式）下应用**不会**访问本服务；
  offline tests 必须保持这一隔离。
