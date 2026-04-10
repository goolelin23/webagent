"""
提示词动态拼装引擎
根据Agent类型、业务上下文和知识库信息动态构建提示词
支持注入深度分析的页面技能和业务流程
"""

from __future__ import annotations
from typing import Any

from webagent.prompt_engine.context import ContextManager, BusinessContext
from webagent.prompt_engine.templates.explorer import (
    EXPLORER_SYSTEM_PROMPT,
    EXPLORER_TASK_TEMPLATE,
    EXPLORER_PAGE_ANALYSIS_PROMPT,
)
from webagent.prompt_engine.templates.planner import (
    PLANNER_SYSTEM_PROMPT,
    PLANNER_TASK_TEMPLATE,
    PLANNER_REPLAN_TEMPLATE,
)
from webagent.prompt_engine.templates.executor import (
    EXECUTOR_SYSTEM_PROMPT,
    EXECUTOR_STEP_TEMPLATE,
    EXECUTOR_ERROR_REPORT_TEMPLATE,
)
from webagent.utils.logger import get_logger

logger = get_logger("webpilot.prompt_engine")


class PromptEngine:
    """提示词动态拼装引擎"""

    def __init__(self, context_manager: ContextManager | None = None):
        self.context_manager = context_manager or ContextManager()

    # ──────────────────────────────────────────────
    # 探索 Agent 提示词
    # ──────────────────────────────────────────────

    def get_explorer_system_prompt(self) -> str:
        """获取探索Agent的系统提示词"""
        base = EXPLORER_SYSTEM_PROMPT
        business_ctx = self.context_manager.get_prompt_context()
        if business_ctx:
            base += f"\n\n## 业务领域上下文：\n{business_ctx}"
        return base

    def build_explorer_task(
        self,
        target_url: str,
        scan_depth: int = 2,
        known_info: str = "",
        additional_instructions: str = "",
    ) -> str:
        """构建探索任务提示词"""
        business_context = self.context_manager.get_prompt_context()
        return EXPLORER_TASK_TEMPLATE.format(
            target_url=target_url,
            scan_depth=scan_depth,
            business_context=business_context or "（暂无业务上下文信息）",
            known_info=known_info or "（首次探索，暂无已知信息）",
            additional_instructions=additional_instructions,
        )

    def build_page_analysis_prompt(self, url: str, title: str) -> str:
        """构建页面分析提示词"""
        return EXPLORER_PAGE_ANALYSIS_PROMPT.format(url=url, title=title)

    # ──────────────────────────────────────────────
    # 规划 Agent 提示词
    # ──────────────────────────────────────────────

    def get_planner_system_prompt(self) -> str:
        """获取规划Agent的系统提示词"""
        base = PLANNER_SYSTEM_PROMPT
        business_ctx = self.context_manager.get_prompt_context()
        if business_ctx:
            base += f"\n\n## 业务领域上下文：\n{business_ctx}"
        return base

    def build_planner_task(
        self,
        user_instruction: str,
        system_info: str = "",
        knowledge_context: str = "",
        available_skills: str = "",
        page_skills: str = "",
        workflows: str = "",
    ) -> str:
        """构建规划任务提示词（含深度分析上下文）"""
        business_context = self.context_manager.get_prompt_context()
        return PLANNER_TASK_TEMPLATE.format(
            user_instruction=user_instruction,
            system_info=system_info or "（暂无系统页面信息）",
            knowledge_context=knowledge_context or "（暂无知识库信息，请根据通用Web操作经验规划）",
            business_context=business_context or "",
            available_skills=available_skills or "（无额外计算技能插件）",
            page_skills=page_skills or "（未扫描，暂无页面技能。请根据通用操作经验规划）",
            workflows=workflows or "（未扫描，暂无业务流程）",
        )

    def build_planner_task_with_deep_context(
        self,
        user_instruction: str,
        system_info: str = "",
        knowledge_context: str = "",
        available_skills: str = "",
        deep_context: dict | None = None,
    ) -> str:
        """
        构建规划任务提示词（自动注入深度分析上下文）

        Args:
            deep_context: 从 KnowledgeStore.get_deep_context() 获取的上下文字典
        """
        page_skills = "（未扫描，暂无页面技能）"
        workflows = "（未扫描，暂无业务流程）"

        if deep_context and deep_context.get("analyzed"):
            page_skills = deep_context.get("page_skills_prompt", "（暂无）")
            workflows = deep_context.get("workflows_prompt", "（暂无）")

            # 补充系统描述到 system_info
            sys_desc = deep_context.get("system_description", "")
            entities = deep_context.get("business_entities", [])
            if sys_desc or entities:
                extra = ""
                if sys_desc:
                    extra += f"\n系统描述: {sys_desc}"
                if entities:
                    extra += f"\n业务实体: {', '.join(entities)}"
                system_info = (system_info or "") + extra

        return self.build_planner_task(
            user_instruction=user_instruction,
            system_info=system_info,
            knowledge_context=knowledge_context,
            available_skills=available_skills,
            page_skills=page_skills,
            workflows=workflows,
        )

    def build_replan_prompt(
        self,
        original_task: str,
        completed_steps: str,
        failed_step: str,
        error_message: str,
        current_state: str,
        page_skills: str = "",
    ) -> str:
        """构建重新规划提示词"""
        return PLANNER_REPLAN_TEMPLATE.format(
            original_task=original_task,
            completed_steps=completed_steps,
            failed_step=failed_step,
            error_message=error_message,
            current_state=current_state,
            page_skills=page_skills or "（暂无页面技能）",
        )

    # ──────────────────────────────────────────────
    # 执行 Agent 提示词
    # ──────────────────────────────────────────────

    def get_executor_system_prompt(self) -> str:
        """获取执行Agent的系统提示词"""
        return EXECUTOR_SYSTEM_PROMPT

    def build_executor_step(
        self,
        step_id: int,
        action: str,
        target: str,
        value: str = "",
        description: str = "",
        timeout: int = 10000,
        current_url: str = "",
        current_title: str = "",
    ) -> str:
        """构建单步执行提示词"""
        return EXECUTOR_STEP_TEMPLATE.format(
            step_id=step_id,
            action=action,
            target=target,
            value=value or "（无需填值）",
            description=description,
            timeout=timeout,
            current_url=current_url,
            current_title=current_title,
        )

    def build_error_report(
        self,
        step_id: int,
        action: str,
        target: str,
        description: str,
        error_type: str,
        error_message: str,
        current_url: str = "",
        current_title: str = "",
        visible_modals: str = "",
    ) -> str:
        """构建错误报告提示词"""
        return EXECUTOR_ERROR_REPORT_TEMPLATE.format(
            step_id=step_id,
            action=action,
            target=target,
            description=description,
            error_type=error_type,
            error_message=error_message,
            current_url=current_url,
            current_title=current_title,
            visible_modals=visible_modals or "无",
        )

    # ──────────────────────────────────────────────
    # 上下文管理快捷方法
    # ──────────────────────────────────────────────

    def set_business_context(self, context: BusinessContext):
        """设置业务上下文"""
        self.context_manager.set_context(context)
        logger.info(f"已设置业务上下文: {context.domain}")

    def load_domain(self, domain: str):
        """加载预定义的业务领域模板"""
        self.context_manager.load_domain_template(domain)
        logger.info(f"已加载业务领域模板: {domain}")
