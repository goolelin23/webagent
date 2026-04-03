"""
Agent 调度器 (Orchestrator)
协调探索、规划、执行三个Agent的工作流
"""

from __future__ import annotations
import asyncio
from typing import Any

from webagent.agents.explorer import ExplorerAgent
from webagent.agents.planner import PlannerAgent
from webagent.agents.executor import ExecutorAgent, ExecutionReport
from webagent.knowledge.store import KnowledgeStore
from webagent.knowledge.models import ExecutionPlan
from webagent.prompt_engine.engine import PromptEngine
from webagent.prompt_engine.context import ContextManager
from webagent.skills.skill_manager import SkillManager
from webagent.safety.classifier import SafetyClassifier
from webagent.safety.audit import AuditLogger
from webagent.utils.logger import (
    get_logger, print_agent, print_success, print_error,
    print_warning, console,
)
from webagent.utils.config import get_config

logger = get_logger("webagent.agents.orchestrator")


class AgentOrchestrator:
    """
    Agent调度器 — 统一管理和协调三个Agent
    """

    def __init__(self):
        self.config = get_config()

        # 初始化核心组件
        self.context_manager = ContextManager()
        self.prompt_engine = PromptEngine(self.context_manager)
        self.knowledge_store = KnowledgeStore()
        self.skill_manager = SkillManager()
        self.safety_classifier = SafetyClassifier(safety_level=self.config.safety.level)
        self.audit_logger = AuditLogger()

        # 初始化三个Agent
        self.explorer = ExplorerAgent(
            prompt_engine=self.prompt_engine,
            knowledge_store=self.knowledge_store,
        )
        self.planner = PlannerAgent(
            prompt_engine=self.prompt_engine,
            knowledge_store=self.knowledge_store,
            skill_manager=self.skill_manager,
        )
        self.executor = ExecutorAgent(
            safety_classifier=self.safety_classifier,
            skill_manager=self.skill_manager,
            audit_logger=self.audit_logger,
        )

        self._max_replan_attempts = 3

    async def run_task(
        self,
        instruction: str,
        target_url: str = "",
        auto_scan: bool = False,
    ) -> ExecutionReport:
        """
        执行完整的自动化任务流程

        流程: 用户指令 → [可选扫描] → 规划 → 执行 → [可能重新规划]

        Args:
            instruction: 用户自然语言指令
            target_url: 目标系统URL
            auto_scan: 是否在执行前先扫描目标系统
        Returns:
            ExecutionReport
        """
        console.print(f"\n[bold cyan]{'='*60}[/bold cyan]")
        console.print(f"[bold cyan]  🚀 开始执行任务[/bold cyan]")
        console.print(f"[bold cyan]{'='*60}[/bold cyan]")
        console.print(f"  指令: {instruction}")
        if target_url:
            console.print(f"  目标: {target_url}")
        console.print()

        try:
            # Step 1: 如果需要，先扫描目标系统
            if auto_scan and target_url:
                console.print("[bold]📡 阶段一: 系统扫描[/bold]")
                await self.scan(target_url)
                console.print()

            # Step 2: 生成执行计划
            console.print("[bold]📋 阶段二: 任务规划[/bold]")
            plan = await self.planner.plan(instruction, target_url)
            console.print()

            # Step 3: 执行计划（含重试和重新规划）
            console.print("[bold]⚡ 阶段三: 任务执行[/bold]")
            report = await self._execute_with_replan(plan)
            console.print()

            # 输出最终报告
            self._print_final_report(report)

            return report

        except Exception as e:
            logger.error(f"任务执行失败: {e}")
            print_error(f"任务执行异常: {e}")
            raise
        finally:
            await self.executor.close()

    async def scan(
        self,
        target_url: str,
        depth: int = 2,
        max_pages: int = 50,
    ):
        """
        扫描目标系统，生成知识库

        Args:
            target_url: 目标URL
            depth: 扫描深度
            max_pages: 最大页面数
        """
        return await self.explorer.scan_site(
            target_url=target_url,
            scan_depth=depth,
            max_pages=max_pages,
        )

    async def plan_only(
        self,
        instruction: str,
        target_url: str = "",
    ) -> ExecutionPlan:
        """
        仅生成执行计划（不执行）

        Args:
            instruction: 用户指令
            target_url: 目标URL
        Returns:
            ExecutionPlan
        """
        return await self.planner.plan(instruction, target_url)

    async def _execute_with_replan(self, plan: ExecutionPlan) -> ExecutionReport:
        """执行计划，失败时自动重新规划"""
        current_plan = plan
        replan_count = 0
        all_completed_steps = []

        while replan_count <= self._max_replan_attempts:
            report = await self.executor.execute_plan(current_plan)

            if report.success:
                return report

            if not report.needs_replan:
                return report

            if replan_count >= self._max_replan_attempts:
                print_warning(f"已达最大重新规划次数 ({self._max_replan_attempts})")
                return report

            # 重新规划
            replan_count += 1
            print_warning(f"第 {replan_count} 次重新规划...")

            # 获取当前状态
            current_state = await self.executor.get_current_state()

            # 收集已完成的步骤
            completed = [
                r.step for r in report.results if r.success
            ]
            all_completed_steps.extend(completed)

            try:
                current_plan = await self.planner.replan(
                    original_plan=plan,
                    completed_steps=all_completed_steps,
                    failed_step=report.failed_step,
                    error_message=report.error_message,
                    current_state=current_state,
                )
            except Exception as e:
                logger.error(f"重新规划失败: {e}")
                return report

        return report

    def set_business_domain(self, domain: str):
        """设置业务领域"""
        self.prompt_engine.load_domain(domain)
        console.print(f"  [cyan]已加载业务领域: {domain}[/cyan]")

    def list_knowledge_bases(self) -> list[str]:
        """列出所有已扫描的站点"""
        return self.knowledge_store.list_sites()

    def get_knowledge_summary(self, domain: str) -> str | None:
        """获取知识库摘要"""
        return self.knowledge_store.get_site_summary(domain)

    async def analyze(self, domain: str) -> str:
        """
        对已扫描的站点进行深度分析（手动触发）

        Args:
            domain: 站点域名
        Returns:
            分析摘要
        """
        site = self.knowledge_store.load(domain)
        if not site:
            return f"未找到站点: {domain}"

        if not site.pages:
            return f"站点 [{domain}] 没有扫描数据，请先执行扫描"

        site = await self.explorer.deep_analyze(site)

        # 注册生成的页面技能
        self._load_page_skills(domain)

        return site.summary()

    def _load_page_skills(self, domain: str):
        """从知识库加载页面技能到 SkillManager"""
        from webagent.skills.page_skill_generator import PageSkillGenerator
        site = self.knowledge_store.load(domain)
        if site and site.is_analyzed:
            generator = PageSkillGenerator(self.skill_manager)
            count = generator.generate_and_register(site)
            if count > 0:
                print_agent("explorer", f"已加载 {count} 个页面技能到技能管理器")

    def get_page_skills_summary(self, domain: str) -> str:
        """获取页面技能摘要"""
        from webagent.skills.page_skill_generator import PageSkillGenerator
        site = self.knowledge_store.load(domain)
        if not site:
            return f"未找到站点: {domain}"
        generator = PageSkillGenerator(self.skill_manager)
        return generator.get_skills_summary(site)

    def list_skills(self) -> list[dict]:
        """列出所有可用技能"""
        return self.skill_manager.list_skills()

    def _print_final_report(self, report: ExecutionReport):
        """打印最终执行报告"""
        console.print(f"[bold cyan]{'='*60}[/bold cyan]")
        if report.success:
            console.print("[bold green]  ✅ 任务执行成功[/bold green]")
        else:
            console.print("[bold red]  ❌ 任务执行失败[/bold red]")

        console.print(f"  任务: {report.plan.task}")
        console.print(f"  步骤: {report.completed_steps}/{report.total_steps} 完成")
        console.print(f"  成功率: {report.success_rate:.0%}")

        if report.error_message:
            console.print(f"  [red]错误: {report.error_message}[/red]")

        # 审计摘要
        audit_summary = self.audit_logger.get_summary()
        console.print(f"  审计: {audit_summary['total_actions']} 条操作记录")
        console.print(f"[bold cyan]{'='*60}[/bold cyan]\n")
