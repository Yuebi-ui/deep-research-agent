#***********************************************
#      Filename: red_team_agent.py
#   Description: Red-Team智能体 
#***********************************************

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import END
from langgraph.types import Command

from deep_research.prompts import RED_TEAM_PROMPT
from deep_research.llm import get_chat_model
from deep_research.states import SupervisorState, Critique
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


# 初始化模型
red_team_model = get_chat_model("red_team")

# CONSTANTS
MAX_CRITIC = 3           # 红队最大批评次数，防止追求完美，无限循环
MIN_DRAFT_LEN = 50      # 报告草稿字数最少不低于50, 否则不予评判，直接返回
MIN_CRITIC = 20          # 如果红队输出很短，则判定没有缺陷（防止模型指令遵循不足输出"PASS"以外的其他字符）


async def red_team_node(state: SupervisorState) -> Command:
    """
    这是一个红队对抗智能体，用于找出报告的逻辑缺陷，漏洞和不完善的地方。
    如果是 final_exit 模式（Supervisor 准备退出前的最终审查），审查后直接跳转到 END。
    """

    draft = state.get("draft_report", "")
    research_brief = state.get("research_brief", "")
    critique_nums = state.get("critique_nums", 0)
    final_exit = state.get("final_exit", False)

    # 最终退出前的红队审查：无论有无 draft 都要执行（用 research_brief 做基础）
    if final_exit and not draft:
        draft = research_brief or "无草稿"

    # 设置最大对抗次数
    if critique_nums >= MAX_CRITIC or not draft or len(draft) < MIN_DRAFT_LEN:
        return Command(goto=END) if final_exit else Command(goto="supervisor")

    # 组装prompt
    prompt = RED_TEAM_PROMPT.format(research_brief=research_brief, draft_report=draft)

    # 调用红队大模型获得对抗建议
    response = await red_team_model.ainvoke([HumanMessage(content=prompt)])
    content = response.content

    # 如果是 final_exit 模式，始终记录审查结果
    if final_exit:
        logger.info(f"[RED TEAM] final review: {content[:200]}")
        if "PASS" in content or len(content) < MIN_CRITIC:
            return Command(goto=END)
        critique = Critique(
            author="Red Team Adversary (Final)",
            concern=content,
            addressed=False
        )
        return Command(
            goto=END,
            update={
                "active_critiques": [critique],
                "supervisor_messages": [
                    SystemMessage(content=f"FINAL ADVERSARIAL FEEDBACK: {content}")
                ]
            }
        )

    # 常规流程：如果"PASS", 则直接返回
    if "PASS" in content or len(content) < MIN_CRITIC:
        return Command(goto="supervisor")

    # 如果找到报告缺陷，返回critique
    critique = Critique(
        author="Red Team Adversary",
        concern=content,
        addressed=False
    )
    logger.info(f"[RED TEAM] {content}")

    # 返回active_critiques实现动态上下文注入，并把该意见作为System Message注入到Supervisor的消息历史中
    return Command(
        goto="supervisor",
        update={
            "active_critiques": [critique],
            "critique_nums": critique_nums + 1,
            "supervisor_messages": [
                SystemMessage(content=f"ADVERSARIAL FEEDBACK DETECTED: {content}")
            ]
        }
    )
