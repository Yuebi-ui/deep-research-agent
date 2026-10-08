"""离线测试替身（Fake LLM / Search / Embedding）。

只在 ``APP_ENV=test`` 或 ``ALLOW_LIVE_EXTERNAL_APIS=false`` 时被运行时惰性导入，
正常启动不会加载本包。
"""

from deep_research.testing.fake_llm import (
    FakeChatModel,
    make_fake_chat_model,
    reset_responders,
    set_responder,
)

__all__ = [
    "FakeChatModel",
    "make_fake_chat_model",
    "reset_responders",
    "set_responder",
]
