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

        # ── 人工介入兜底 (Human-in-the-loop) ──
        if not retry_result.success:
            try:
                import sys
                if sys.stdin.isatty():
                    from rich.prompt import Confirm
                    from webagent.utils.logger import console
                    console.print(f"\n[bold yellow]⚠️ 步骤执行受阻或点选失败: {step.description}[/bold yellow]")
                    console.print(f"[dim]原因为: {retry_result.last_error}[/dim]")
                    
                    loop = asyncio.get_event_loop()
                    should_intervene = await loop.run_in_executor(
                        None, 
                        lambda: Confirm.ask("是否需要人工介入辅助操作？(将暂停自动化并允许您在浏览器内手工点击)")
                    )
                    
                    if should_intervene:
                        console.print("[cyan]👉 已开启 Playwright 检查器，浏览器处于暂停状态。[/cyan]")
                        console.print("[cyan]请在浏览器中手工完成该操作，完成后点击浮动栏上的 'Resume / 恢复' 按钮。[/cyan]")
                        
                        # 注入前端嗅探器，精准监听用户的人工点击动作以实现纠错自学习
                        await self.page.evaluate("""
                            window.__human_tracker = null;
                            window.__ht_listener = (e) => {
                                if (e.isTrusted) {
                                    window.__human_tracker = {
                                        x: e.clientX,
                                        y: e.clientY,
                                        tag: e.target.tagName,
                                        id: e.target.id || '',
                                        class: e.target.className || '',
                                        text: (e.target.textContent || '').trim().substring(0, 40)
                                    };
                                }
                            };
                            document.body.addEventListener('click', window.__ht_listener, true);
                        """)

                        await self.page.pause()
                        
                        # 提取用户留下的纠错轨迹数据
                        tracked_data = await self.page.evaluate("""
                            (() => {
                                document.body.removeEventListener('click', window.__ht_listener, true);
                                return window.__human_tracker;
                            })()
                        """)
                        
                        if tracked_data:
                            # 提取用户的纠错坐标与目标特征，并固化为管线日志的 step.value 
                            step.value = f"[HUMAN_CORRECTION] x:{tracked_data['x']}, y:{tracked_data['y']}, tag:{tracked_data['tag']}, text:{tracked_data['text']}"
                            console.print(f"[green]✅ 已捕获人类导师纠错动作坐标: ({tracked_data['x']}, {tracked_data['y']})，组件特征已记入模型！[/green]")
                        else:
                            step.value = "[HUMAN_INTERVENED]"
                            console.print("[green]✅ 收到恢复信号，未捕获点击，视作接管成功继续。[/green]")

                        retry_result.success = True
                        retry_result.last_error = ""
                        retry_result.should_replan = False
            except Exception as e:
                logger.debug(f"人工介入交互异常: {e}")

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
        当选择器匹配 0 个元素时返回 None，由调用方走视觉降级
        """
        locator = self.page.locator(selector)
        count = await locator.count()
        if count == 0:
            logger.warning(f"选择器 [{selector}] 未找到任何元素，将触发视觉降级")
            return None
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
                x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
                confidence = data.get("confidence", 0)
                desc = data.get("element_description", "")
                if x > 0 and y > 0 and confidence >= 0.5:
                    # 对视觉坐标进行 DOM 精修吸附
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

    async def _execute_action(self, step: ExecutionStep):
        """执行具体的页面操作 — 选择器优先，找不到自动走视觉降级"""
        action = step.action
        target = step.target
        value = step.value
        timeout = step.timeout

        if action == "navigate":
            await self.page.goto(target, wait_until="domcontentloaded", timeout=timeout)

        elif action == "click":
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click(timeout=timeout)
            else:
                # 视觉降级：截图找元素 → 坐标点击
                coords = await self._vision_locate(target)
                if coords:
                    from webagent.agents.vision_engine import VisionEngine
                    engine = VisionEngine()
                    await engine._smart_click(self.page, coords["x"], coords["y"])
                    await VisionEngine._wait_stable(self.page)
                else:
                    raise ValueError(f"元素未找到且视觉降级也无法定位: {target}")

        elif action == "fill":
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click(click_count=3, timeout=timeout)
                await self.page.keyboard.press("Backspace")
                await locator.fill(value)
            else:
                # 视觉降级：截图找输入框 → 坐标点击后键盘输入
                coords = await self._vision_locate(target)
                if coords:
                    from webagent.agents.vision_engine import VisionEngine
                    engine = VisionEngine()
                    await engine._smart_fill(self.page, coords["x"], coords["y"], value)
                    await VisionEngine._wait_stable(self.page)
                else:
                    raise ValueError(f"输入框未找到且视觉降级也无法定位: {target}")

        elif action == "select":
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                await locator.select_option(value, timeout=timeout)
            else:
                # 视觉降级：先点开下拉框，再选
                coords = await self._vision_locate(target)
                if coords:
                    await self.page.mouse.click(coords["x"], coords["y"])
                    from webagent.agents.vision_engine import VisionEngine
                    await VisionEngine._wait_stable(self.page)
                    # 尝试用文本匹配选项
                    option_coords = await self._vision_locate(f"下拉选项: {value}")
                    if option_coords:
                        await self.page.mouse.click(option_coords["x"], option_coords["y"])
                        await VisionEngine._wait_stable(self.page)
                else:
                    raise ValueError(f"下拉框未找到且视觉降级也无法定位: {target}")

        elif action == "check":
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                await locator.check(timeout=timeout)
            else:
                coords = await self._vision_locate(target)
                if coords:
                    await self.page.mouse.click(coords["x"], coords["y"])
                    from webagent.agents.vision_engine import VisionEngine
                    await VisionEngine._wait_stable(self.page)
                else:
                    raise ValueError(f"复选框未找到且视觉降级也无法定位: {target}")

        elif action == "wait":
            if target:
                await self.page.wait_for_selector(target, timeout=timeout)
            else:
                await asyncio.sleep(float(value) / 1000 if value else 1.0)

        elif action == "scroll":
            if target:
                locator = await self._safe_locator(target, timeout)
                if locator is not None:
                    await locator.scroll_into_view_if_needed()
                else:
                    coords = await self._vision_locate(target)
                    if coords:
                        await self.page.mouse.move(coords["x"], coords["y"])
                        await self.page.mouse.wheel(0, 300)
                    else:
                        await self.page.evaluate("window.scrollBy(0, 300)")
            else:
                await self.page.evaluate("window.scrollBy(0, 300)")

        elif action == "screenshot":
            path = target or f"screenshots/step_{step.step_id}.png"
            await self.page.screenshot(path=path)

        elif action == "assert":
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                text = await locator.text_content()
            else:
                # 视觉降级：对整个页面截图让 LLM 判断
                text = await self.page.evaluate("document.body.innerText || ''")
            if value and value not in (text or ""):
                raise AssertionError(f"断言失败: 期望包含 '{value}', 实际文本中未找到")

        elif action == "type":
            # 模拟逐字输入（适用于某些输入框）
            locator = await self._safe_locator(target, timeout)
            if locator is not None:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click()
                await self.page.keyboard.type(value, delay=50)
            else:
                coords = await self._vision_locate(target)
                if coords:
                    await self.page.mouse.click(coords["x"], coords["y"])
                    from webagent.agents.vision_engine import VisionEngine
                    await VisionEngine._wait_stable(self.page)
                    await self.page.keyboard.type(value, delay=50)
                else:
                    raise ValueError(f"输入框未找到且视觉降级也无法定位: {target}")

        elif action == "press":
            await self.page.keyboard.press(value or "Enter")

        elif action == "click_xy":
            # 视觉坐标点击
            coords = json.loads(target) if isinstance(target, str) else target
            x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
            await self.page.mouse.click(x, y)
            from webagent.agents.vision_engine import VisionEngine
            await VisionEngine._wait_stable(self.page)

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
                # 视觉检查：每次滚动后用视觉模型找
                coords = await self._vision_locate(target)
                if coords:
                    await self.page.mouse.move(coords["x"], coords["y"])
                    break
                await self.page.evaluate("window.scrollBy(0, 300)")
                from webagent.agents.vision_engine import VisionEngine
                await VisionEngine._wait_stable(self.page)

        elif action == "vision_fill":
            # 视觉填写：基于坐标点击输入框再输入
            coords = json.loads(target) if isinstance(target, str) else target
            x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
            await self.page.mouse.click(x, y, click_count=3)
            from webagent.agents.vision_engine import VisionEngine
            await VisionEngine._wait_stable(self.page)
            await self.page.keyboard.press("Backspace")
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
