"""Benchmark 可复现性基建（Phase 3C P1）。

- :mod:`deep_research.benchmark.fingerprint`：代码 revision / 配置指纹 /
  experiment identity / 运行期 freeze 判定；
- :mod:`deep_research.benchmark.preflight`：benchmark 启动前 self-check，
  与实验预期不一致时 FAIL FAST。
"""

from deep_research.benchmark.fingerprint import (  # noqa: F401
    ExperimentIdentity,
    build_config_snapshot,
    collect_code_revision,
    config_fingerprint,
    evaluate_run_validity,
    scrub_secrets,
)
from deep_research.benchmark.preflight import (  # noqa: F401
    Expectations,
    run_preflight,
)
