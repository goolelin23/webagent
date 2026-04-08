"""
工具管线核心
连接Agent与实际网页的桥梁，校验状态、安全过滤、执行操作、错误处理
"""

from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime
from playwright.async_api import Page

from webagent.pipeline.state_validator import StateValidator, ValidationResult
from webagent.pipeline.retry_manager import RetryManager, RetryResult
from webagent.knowledge.models import ExecutionStep
from webagent.utils.logger import get_logger, print_step, print_success, print_error, print_warning

logger = get_logger("webagent.pipeline")


@dataclass
class ActionResult:
    """操作执行结果"""
    success: bool
    step: ExecutionStep
    message: str = ""
    screenshot_path: str = ""
    page_state: dict = field(default_factory=dict)
    duration_ms: int = 0
    retries: int = 0
    needs_replan: bool = False
    error_type: str = ""
    error_message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class ActionPipeline:
    """操作管线 — 每个Agent动作都必须经过此管线"""

    def __init__(
        self,
        page: Page,
        safety_classifier=None,
        skill_manager=None,
    ):
        self.page = page
        self.validator = StateValidator(page)
        self.retry_manager = RetryManager()
        self.safety_classifier = safety_classifier
        self.skill_manager = skill_manager
        self._action_log: list[ActionResult] = []

        # 注册恢复操作
        self.retry_manager.register_recovery("dismiss_modal", self._recovery_dismiss_modal)
        self.retry_manager.register_recovery("refresh", self._recovery_refresh)
        self.retry_manager.register_recovery("scroll_and_retry", self._recovery_scroll)
        self.retry_manager.register_recovery("wait_and_retry", self._recovery_wait)
        self.retry_manager.register_recovery("clear_and_refill", self._recovery_wait)

    async def execute_step(self, step: ExecutionStep) -> ActionResult:
        """
        通过管线执行单个步骤

        流程: 前置校验 → 安全过滤 → 操作执行 → 后置校验
        """
        start_time = datetime.now()

        # ── 1. 前置校验 ──
        pre_validation = await self._pre_validate(step)
        if not pre_validation.passed:
            # 尝试自动恢复
            recovered = await self._try_recover(pre_validation)
            if not recovered:
                result = ActionResult(
                    success=False,
                    step=step,
                    message=f"前置校验失败: {pre_validation.message}",
                    error_type="pre_validation_failed",
                    error_message=pre_validation.message,
                    needs_replan=not pre_validation.recoverable,
                )
                self._action_log.append(result)
                return result

        # ── 2. 安全过滤 ──
        if self.safety_classifier:
            safety_result = await self.safety_classifier.classify_action(step)
            if safety_result.get("blocked"):
                result = ActionResult(
                    success=False,
                    step=step,
                    message=f"安全拦截: {safety_result.get('reason', '高风险操作')}",
                    error_type="safety_blocked",
                    error_message=safety_result.get("reason", ""),
                )
                self._action_log.append(result)
                print_warning(f"操作被安全策略拦截: {safety_result.get('reason')}")
                return result

        # ── 3. 技能插件处理 ──
        if step.skill and self.skill_manager:
            try:
                skill_result = await self.skill_manager.execute_skill(
                    step.skill, step.skill_params
                )
                if skill_result.success:
                    step.value = str(skill_result.value)
                    logger.info(f"技能 [{step.skill}] 计算结果: {step.value}")
            except Exception as e:
                logger.warning(f"技能 [{step.skill}] 执行失败: {e}")

        # ── 4. 执行操作（带重试） ──
        retry_result = await self.retry_manager.execute_with_retry(
            operation=lambda: self._execute_action(step),
            recovery_action=pre_validation.recovery_action if not pre_validation.passed else "",
            description=f"步骤{step.step_id}: {step.description}",
        )

        # ── 5. 后置校验 ──
        post_state = await self.validator.get_page_state()

        duration = (datetime.now() - start_time).total_seconds() * 1000

        if retry_result.success:
            # 对 fill 操作进行值验证
            if step.action == "fill" and step.target:
                fill_check = await self.validator.validate_fill_result(
                    step.target, step.value
                )
                if not fill_check.passed:
                    logger.warning(f"填值验证失败: {fill_check.message}")

            result = ActionResult(
                success=True,
                step=step,
                message="操作成功",
                page_state=post_state,
                duration_ms=int(duration),
                retries=retry_result.attempts - 1,
            )
            print_success(f"步骤{step.step_id}: {step.description}")
        else:
            result = ActionResult(
                success=False,
                step=step,
                message=retry_result.last_error,
                page_state=post_state,
                duration_ms=int(duration),
                retries=retry_result.attempts,
                needs_replan=retry_result.should_replan,
                error_type="execution_failed",
                error_message=retry_result.last_error,
            )
            print_error(f"步骤{step.step_id}: {step.description} — {retry_result.last_error}")

        self._action_log.append(result)
        return result

    async def _execute_action(self, step: ExecutionStep):
        """执行具体的页面操作"""
        action = step.action
        target = step.target
        value = step.value
        timeout = step.timeout

        if action == "navigate":
            await self.page.goto(target, wait_until="domcontentloaded", timeout=timeout)

        elif action == "click":
            locator = self.page.locator(target)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click(timeout=timeout)

        elif action == "fill":
            locator = self.page.locator(target)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.clear()
            await locator.fill(value)

        elif action == "select":
            locator = self.page.locator(target)
            await locator.select_option(value, timeout=timeout)

        elif action == "check":
            locator = self.page.locator(target)
            await locator.check(timeout=timeout)

        elif action == "wait":
            if target:
                await self.page.wait_for_selector(target, timeout=timeout)
            else:
                await asyncio.sleep(float(value) / 1000 if value else 1.0)

        elif action == "scroll":
            if target:
                locator = self.page.locator(target)
                await locator.scroll_into_view_if_needed()
            else:
                await self.page.evaluate("window.scrollBy(0, 300)")

        elif action == "screenshot":
            path = target or f"screenshots/step_{step.step_id}.png"
            await self.page.screenshot(path=path)

        elif action == "assert":
            locator = self.page.locator(target)
            text = await locator.text_content()
            if value and value not in (text or ""):
                raise AssertionError(f"断言失败: 期望包含 '{value}', 实际 '{text}'")

        elif action == "type":
            # 模拟逐字输入（适用于某些输入框）
            locator = self.page.locator(target)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            await self.page.keyboard.type(value, delay=50)

        elif action == "press":
            await self.page.keyboard.press(value or "Enter")

        elif action == "click_xy":
            # 视觉坐标点击
            coords = json.loads(target) if isinstance(target, str) else target
            x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.5)

        elif action == "scroll_to_find":
            # 持续滚动 + 视觉查找
            max_scrolls = 10
            for _ in range(max_scrolls):
                try:
                    locator = self.page.locator(target)
                    if await locator.count() > 0 and await locator.first.is_visible():
                        await locator.first.scroll_into_view_if_needed()
                        break
                except Exception:
                    pass
                await self.page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.5)

        elif action == "vision_fill":
            # 视觉填写：基于坐标点击输入框再输入
            coords = json.loads(target) if isinstance(target, str) else target
            x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.3)
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.type(value, delay=30)

        elif action == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=timeout)

        else:
            raise ValueError(f"不支持的操作类型: {action}")

    async def _pre_validate(self, step: ExecutionStep) -> ValidationResult:
        """前置校验"""
        # 检查页面是否加载
        page_loaded = await self.validator.validate_page_loaded()
        if not page_loaded.passed:
            return page_loaded

        # 对需要操作元素的动作进行元素就绪检查
        if step.action in ("click", "fill", "select", "check", "type") and step.target:
            # 先检查弹窗
            modal_check = await self.validator.check_for_modals()
            if not modal_check.passed:
                return modal_check

            # 检查元素
            elem_check = await self.validator.validate_element_ready(
                step.target, timeout=step.timeout
            )
            return elem_check

        return ValidationResult(passed=True)

    async def _try_recover(self, validation: ValidationResult) -> bool:
        """尝试自动恢复"""
        if not validation.recoverable:
            return False

        action = validation.recovery_action
        if action == "dismiss_modal":
            return await self.validator.dismiss_modal()
        elif action == "refresh":
            await self.page.reload()
            await asyncio.sleep(2)
            return True
        elif action == "scroll_and_retry":
            await self.page.evaluate("window.scrollBy(0, 300)")
            await asyncio.sleep(1)
            return True
        elif action == "wait_and_retry":
            await asyncio.sleep(3)
            return True

        return False

    # ── 恢复操作处理器 ──

    async def _recovery_dismiss_modal(self):
        await self.validator.dismiss_modal()

    async def _recovery_refresh(self):
        await self.page.reload()
        await asyncio.sleep(2)

    async def _recovery_scroll(self):
        await self.page.evaluate("window.scrollBy(0, 300)")
        await asyncio.sleep(1)

    async def _recovery_wait(self):
        await asyncio.sleep(3)

    # ── 查询方法 ──

    @property
    def action_log(self) -> list[ActionResult]:
        """获取操作日志"""
        return self._action_log.copy()

    def get_success_rate(self) -> float:
        """获取操作成功率"""
        if not self._action_log:
            return 0.0
        success_count = sum(1 for r in self._action_log if r.success)
        return success_count / len(self._action_log)
