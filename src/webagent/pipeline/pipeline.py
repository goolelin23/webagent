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

logger = get_logger("webpilot.pipeline")

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
        # 策略: 选择器操作仅尝试一次，失败直接降级到视觉引擎，不做重试
        from webagent.pipeline.retry_manager import RetryPolicy
        self.retry_manager = RetryManager(policy=RetryPolicy(max_retries=1))
        self.safety_classifier = safety_classifier
        self.skill_manager = skill_manager
        self._action_log: list[ActionResult] = []

        # 注册恢复操作（保留以备特殊场景手动调用）
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
        # 执行链路:选择器操作(重试) → 视觉降级(自动) → 询问用户(最后手段)
        try:
            retry_result = await self.retry_manager.execute_with_retry(
                operation=lambda: self._execute_action(step),
                recovery_action=pre_validation.recovery_action if not pre_validation.passed else "",
                description=f"步骤{step.step_id}: {step.description}",
            )
        except Exception as e:
            # this shouldn't happen unless execute_with_retry itself fails
            retry_result = RetryResult(success=False, attempts=1, last_error=str(e))

        # ── 5. 视觉降级 (Vision Fallback) ──
        vision_failed = False
        if not retry_result.success and step.action in ["click", "fill", "select", "check", "scroll", "type"]:
            logger.warning("选择器操作失败，跳过重试，直接降级到视觉引擎定位...")
            try:
                # 只在重试彻底失败的最后时刻调用一回大模型视觉定位
                vision_success = await self._execute_vision_action(step)
                if vision_success:
                    retry_result.success = True
                    retry_result.last_error = ""
                    retry_result.should_replan = False
                    logger.info("视觉降级定位并操作成功！")
                else:
                    vision_failed = True
                    retry_result.last_error = "视觉降级定位无法找到可操作的界面元素"
            except Exception as e:
                vision_failed = True
                retry_result.last_error = f"视觉降级执行异常: {e}"

        # ── 6. 人工介入兜底 (Human-in-the-loop) ──
        if not retry_result.success:
            from webagent.utils.logger import console

            if retry_result.last_error:
                if "vision_failed" in retry_result.last_error:
                    console.print(f"\n[bold yellow]⚠️ 选择器和视觉模型均无法定位元素: {step.description}[/bold yellow]")
                    console.print(f"[dim]目标: {step.target}[/dim]")
                else:
                    console.print(f"\n[bold yellow]⚠️ 步骤执行受阻或点选失败: {step.description}[/bold yellow]")
                    console.print(f"[dim]原因为: {retry_result.last_error}[/dim]")

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

    async def _safe_locator(self, selector: str, timeout: int = 10000):
        """
        安全定位器：当选择器匹配多个元素时自动降级到 .first
        当选择器匹配 0 个元素时等待直至超时，抛出异常
        """
        locator = self.page.locator(selector)
        try:
            # 尝试等待元素进入 DOM
            await locator.first.wait_for(state="attached", timeout=timeout)
        except Exception:
            raise ValueError(f"DOM 选择器在 {timeout}ms 内未能找到元素: {selector}")
            
        count = await locator.count()
        if count > 1:
            logger.warning(
                f"选择器 [{selector}] 匹配到 {count} 个元素，自动选取第一个可见元素"
            )
            # 尝试找到第一个可见的元素
            for i in range(count):
                nth = locator.nth(i)
                try:
                    if await nth.is_visible():
                        return nth
                except Exception:
                    continue
            # 所有元素都不可见，返回第一个
            return locator.first
        return locator

    async def _vision_locate(self, element_description: str) -> dict | None:
        """
        视觉降级定位：当 CSS 选择器找不到元素时，通过截图 + 视觉模型精确定位元素坐标
        返回 {"x": int, "y": int} 或 None
        """
        from webagent.agents.vision_engine import VisionEngine
        from webagent.prompt_engine.templates.vision import VISION_LOCATE_PROMPT

        try:
            vision = VisionEngine()
            screenshot_path = await vision._screenshot(self.page, "locate", draw_som=False)
            prompt = VISION_LOCATE_PROMPT.format(element_description=element_description)
            response = await vision._call_vision_llm(prompt, [screenshot_path])
            data = vision._extract_json(response)

            if data and data.get("found"):
                coords = data.get("coordinates", {})
                llm_x, llm_y = float(coords.get("x", 0)), float(coords.get("y", 0))
                confidence = data.get("confidence", 0)
                desc = data.get("element_description", "")
                
                if llm_x > 0 and llm_y > 0 and confidence >= 0.5:
                    # Step A: 一阶映射 — 将 LLM 截图坐标缩放为 CSS 逻辑像素
                    x, y = await vision._scale_llm_coords(self.page, llm_x, llm_y, screenshot_path)

                    # Step B: 二阶精修 — 以粗略坐标为中心裁剪局部截图放大后重新定位
                    # 只在置信度不够时再调用 VLM（避免浪费 Token）
                    zoom_x, zoom_y, zoom_conf = await vision._zoom_refine_coords(
                        self.page, x, y, element_description
                    )
                    if zoom_conf >= 0.6:
                        # 二阶定位成功且置信度足够，直接采用
                        x, y = zoom_x, zoom_y
                        logger.debug(f"🔬 采用二阶精修坐标: ({x},{y}) conf={zoom_conf:.0%}")
                    else:
                        logger.debug(f"🔬 二阶精修置信度不足({zoom_conf:.0%})，保留一阶坐标({x},{y})")

                    # 记录此时的视觉换算出来的原始坐标 (用于稍后人工对抗误差测量)
                    self._last_vision_coords = {"x": x, "y": y}

                    # Step C: DOM 精修吸附 — elementFromPoint 吸附到真实元素几何中心
                    rx, ry, _sel, _method = await vision._refine_coordinates(
                        self.page, x, y, element_description
                    )
                    print_warning(
                        f"🔭 视觉降级定位成功: '{desc}' → ({rx}, {ry}) 置信度={confidence:.0%}"
                    )
                    return {"x": rx, "y": ry}
                else:
                    logger.warning(f"视觉定位置信度不足: {confidence:.0%}, 坐标=({x},{y})")
        except Exception as e:
            logger.warning(f"视觉降级定位失败: {e}")

        return None

    async def _execute_vision_action(self, step: ExecutionStep) -> bool:
        """执行视觉降级操作"""
        action = step.action
        target = step.target
        value = step.value
        
        coords = await self._vision_locate(target)
        if not coords:
            return False
            
        from webagent.agents.vision_engine import VisionEngine
        engine = VisionEngine()
        
        try:
            if action == "click":
                await engine._smart_click(self.page, coords["x"], coords["y"])
            elif action == "fill":
                await engine._smart_fill(self.page, coords["x"], coords["y"], value)
            elif action == "select":
                await self.page.mouse.click(coords["x"], coords["y"])
                await VisionEngine._wait_stable(self.page)
                option_coords = await self._vision_locate(f"下拉选项: {value}")
                if option_coords:
                    await self.page.mouse.click(option_coords["x"], option_coords["y"])
                else:
                    return False
            elif action == "check":
                await self.page.mouse.click(coords["x"], coords["y"])
            elif action == "type":
                await self.page.mouse.click(coords["x"], coords["y"])
                await VisionEngine._wait_stable(self.page)
                await self.page.keyboard.type(value, delay=50)
            elif action == "scroll":
                await self.page.mouse.move(coords["x"], coords["y"])
                await self.page.mouse.wheel(0, 300)
            elif action == "scroll_to_find":
                await self.page.mouse.move(coords["x"], coords["y"])
            else:
                return False
                
            await VisionEngine._wait_stable(self.page)
            return True
        except Exception as e:
            logger.warning(f"视觉降级操作执行异常: {e}")
            return False

    async def _execute_action(self, step: ExecutionStep):
        """执行具体的页面操作 — 纯 DOM 选择器执行，失败将抛出异常并由重试管理器接管"""
        action = step.action
        target = step.target
        value = step.value
        timeout = step.timeout

        if action == "navigate":
            await self.page.goto(target, wait_until="domcontentloaded", timeout=timeout)

        elif action == "click":
            locator = await self._safe_locator(target, timeout)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click(timeout=timeout)

        elif action == "fill":
            locator = await self._safe_locator(target, timeout)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click(click_count=3, timeout=timeout)
            await self.page.keyboard.press("Backspace")
            await locator.fill(value)

        elif action == "select":
            locator = await self._safe_locator(target, timeout)
            await locator.select_option(value, timeout=timeout)

        elif action == "check":
            locator = await self._safe_locator(target, timeout)
            await locator.check(timeout=timeout)

        elif action == "wait":
            if target:
                await self.page.wait_for_selector(target, timeout=timeout)
            else:
                await asyncio.sleep(float(value) / 1000 if value else 1.0)

        elif action == "scroll":
            if target:
                locator = await self._safe_locator(target, timeout)
                await locator.scroll_into_view_if_needed()
            else:
                await self.page.evaluate("window.scrollBy(0, 300)")

        elif action == "screenshot":
            path = target or f"screenshots/step_{step.step_id}.png"
            await self.page.screenshot(path=path)

        elif action == "assert":
            try:
                locator = await self._safe_locator(target, timeout)
                text = await locator.text_content()
            except Exception:
                text = await self.page.evaluate("document.body.innerText || ''")
            if value and value not in (text or ""):
                raise AssertionError(f"断言失败: 期望包含 '{value}', 实际文本中未找到")

        elif action == "type":
            locator = await self._safe_locator(target, timeout)
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            await self.page.keyboard.type(value, delay=50)

        elif action == "press":
            await self.page.keyboard.press(value or "Enter")

        elif action == "scroll_to_find":
            max_scrolls = 10
            for _ in range(max_scrolls):
                try:
                    locator = self.page.locator(target)
                    if await locator.count() > 0 and await locator.first.is_visible():
                        await locator.first.scroll_into_view_if_needed()
                        return
                except Exception:
                    pass
                await self.page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.3)
            raise ValueError(f"滚动未找到元素: {target}")

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
