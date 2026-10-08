"""Worker Runtime 原语。

职责划分（见 docs/phase-g-runtime-design.md）：

```text
redis.py      连接与配置解析
identity.py   worker 身份
claim.py      Atomic Claim（任务执行的唯一互斥）
heartbeat.py  claim 续约
queue.py      Job transport（Redis Stream）
events.py     事件发布（Redis Stream）与 sequence 分配
runner.py     单个 job 的执行
```

**本包不依赖 FastAPI**，只依赖 `deep_research`（引擎）与 `backend.db`
（仓储），因此可被独立 worker 进程直接使用。
"""
