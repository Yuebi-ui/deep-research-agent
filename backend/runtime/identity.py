"""Worker 身份。

格式（执行包 §10 明令）：

```text
{hostname}:{pid}:{uuid4 前 8 位}
```

**不能只用 PID** —— 容器与 PID 复用会撞，导致两个不同的 worker 进程
拿到相同的身份标识，进而破坏「只有 owner 能续约/释放 claim」的判断。
"""

from __future__ import annotations

import os
import socket
import uuid


def new_worker_id() -> str:
    """生成一个新的 worker 标识。"""
    hostname = socket.gethostname() or "unknown-host"
    return f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
