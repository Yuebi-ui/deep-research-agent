"""Phase 4A：只读 GPU / vLLM 基线 profiling。

原则（与 Phase 4A 任务书一致）：

- **只读**：不修改任何执行语义、不写 task DB / memory、不改 vLLM 配置；
- 采样与解析全部是纯函数（可测试），采集失败不得影响被观测的工作流；
- 只记录数值与函数名，**不记录任何请求/响应文本**。
"""

from deep_research.profiling.scrape import (  # noqa: F401
    HISTOGRAM_METRICS,
    SCALAR_METRICS,
    extract_snapshot,
    histogram_quantile,
    parse_prometheus,
)
from deep_research.profiling.gpu import query_gpu  # noqa: F401
