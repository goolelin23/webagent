"""
执行Agent
调用 Playwright，将规划Agent给出的指令转化为精准的页面操作
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from webagent.knowledge.models import ExecutionPlan, ExecutionStep
from webagent.pipeline.pipeline import ActionPipeline, ActionResult
from webagent.safety.classifier import SafetyClassifier
from webagent.safety.audit import AuditLogger
from webagent.skills.skill_manager import SkillManager
from webagent.utils.logger import (
    get_logger, print_agent, print_step, print_success,
    print_error, print_warning, console,
)
from webagent.utils.config import get_config

logger = get_logger("webpilot.agents.executor")


@dataclass
class ExecutionReport:
    """执行报告"""
    plan: ExecutionPlan
    results: list[ActionResult] = field(default_factory=list)
    success: bool = False
    total_steps: int = 0
    completed_steps: int = 0
    failed_step: ExecutionStep | None = None
    error_message: str = ""
    needs_replan: bool = False
    screenshots: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_steps == 0:
            return 0.0
        return self.completed_steps / self.total_steps

    def summary(self) -> str:
        status = "✅ 成功" if self.success else "❌ 失败"
        lines = [
            f"执行报告 — {status}",
            f"任务: {self.plan.task}",
            f"完成: {self.completed_steps}/{self.total_steps} 步",
            f"成功率: {self.success_rate:.0%}",
        ]
        if self.error_message:
            lines.append(f"错误: {self.error_message}")
        return "\n".join(lines)


class ExecutorAgent:
    """
    执行Agent — 调用 Playwright 执行页面操作
    不关心业务逻辑，只负责精准执行
    """

    def __init__(
        self,
        safety_classifier: SafetyClassifier | None = None,
        skill_manager: SkillManager | None = None,
        audit_logger: AuditLogger | None = None,
    ):
        self.config = get_config()
        self.safety_classifier = safety_classifier
        self.skill_manager = skill_manager
        self.audit_logger = audit_logger or AuditLogger()
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._pipeline: ActionPipeline | None = None
        self._is_parasitic: bool = False  # 寄生模式标记：不拥有浏览器生命周期

    async def initialize(self):
        """初始化浏览器"""
        from playwright.async_api import async_playwright

        print_agent("executor", "初始化浏览器...")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.browser.headless,
        )
        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.browser.viewport_width,
                "height": self.config.browser.viewport_height,
            },
        )
        self._page = await self._context.new_page()

        # 初始化管线
        self._pipeline = ActionPipeline(
            page=self._page,
            safety_classifier=self.safety_classifier,
            skill_manager=self.skill_manager,
        )

        print_agent("executor", "浏览器初始化完成")

    def attach_page(self, page: Page):
        """寄生接管模式：附加到现有的浏览器 Page 上，而不自己创建一个"""
        self._page = page
        self._context = page.context
        self._browser = self._context.browser
        self._is_parasitic = True
        # 此模式下不对 _playwright 赋值，因为我们不是其生命周期的持有者

        self._pipeline = ActionPipeline(
            page=self._page,
            safety_classifier=self.safety_classifier,
            skill_manager=self.skill_manager,
        )
        print_agent("executor", "✨ 成功附加到现有的浏览器 Page (寄生模式)")

    def detach_page(self):
        """解除寄生模式，清理引用但不关闭浏览器"""
        self._page = None
        self._context = None
        self._browser = None
        self._pipeline = None
        self._is_parasitic = False
        print_agent("executor", "🔌 已解除寄生模式，浏览器还给主探索器")

    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionReport:
        """
        执行完整的执行计划

        Args:
            plan: 执行计划
        Returns:
            ExecutionReport 执行报告
        """
        if not self._pipeline:
            await self.initialize()

        report = ExecutionReport(
            plan=plan,
            total_steps=len(plan.steps),
        )

        print_agent("executor", f"开始执行计划: {plan.task}")
        print_agent("executor", f"共 {len(plan.steps)} 个步骤")
        console.print()

        completed_steps: list[ExecutionStep] = []

        for i, step in enumerate(plan.steps):
            print_step(i + 1, len(plan.steps), step.description)

            # 通过管线执行
            result = await self._pipeline.execute_step(step)
            report.results.append(result)

            # 记录审计日志
            self.audit_logger.log_action(
                agent="executor",
                action=step.action,
                target=step.target,
                value=step.value,
                result="success" if result.success else "failed",
                page_url=result.page_state.get("url", ""),
                duration_ms=result.duration_ms,
            )

            if result.success:
                report.completed_steps += 1
                completed_steps.append(step)
            else:
                if step.optional:
                    print_warning(f"可选步骤跳过: {step.description}")
                    continue

                report.failed_step = step
                report.error_message = result.error_message
                report.needs_replan = result.needs_replan

                if result.needs_replan:
                    print_warning("需要重新规划执行路径")
                break

            # 步骤间智能等待（DOM 稳定检测代替固定 sleep）
            try:
                from webagent.agents.vision_engine import VisionEngine
                await VisionEngine._wait_stable(self._page, timeout=2000)
            except Exception:
                await asyncio.sleep(0.3)

        # 判断整体结果
        report.success = report.completed_steps == report.total_steps
        console.print()

        if report.success:
            print_success(f"执行完成 — {report.completed_steps}/{report.total_steps} 步全部成功")
        else:
            print_error(
                f"执行未完成 — {report.completed_steps}/{report.total_steps} 步, "
                f"失败于步骤 {report.failed_step.step_id if report.failed_step else '?'}"
            )

        # 保存审计日志
        self.audit_logger.save()

        return report

    async def execute_single_step(self, step: ExecutionStep) -> ActionResult:
        """执行单个步骤"""
        if not self._pipeline:
            await self.initialize()

        return await self._pipeline.execute_step(step)

    async def get_current_state(self) -> dict:
        """获取当前页面状态"""
        if self._page:
            return await self._pipeline.validator.get_page_state()
        return {"error": "浏览器未初始化"}

    async def take_screenshot(self, path: str = "") -> str:
        """截图"""
        if not self._page:
            raise RuntimeError("浏览器未初始化")

        import time
        if not path:
            path = f"screenshots/screenshot_{int(time.time())}.png"

        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=path)
        return path

    async def close(self):
        """关闭浏览器（寄生模式下仅解除引用，不关闭外部浏览器）"""
        if self._is_parasitic:
            self.detach_page()
            return
        if self._browser:
            await self._browser.close()
            self._browser = None
        if hasattr(self, '_playwright') and self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._pipeline = None
        self._page = None
        self._context = None
        print_agent("executor", "浏览器已关闭")

    @property
    def page(self) -> Page | None:
        return self._page
