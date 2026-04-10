"""
页面状态校验器
在每个操作前后校验页面状态，确保操作的前置条件满足
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any
from playwright.async_api import Page, Locator
from webagent.utils.logger import get_logger

logger = get_logger("webpilot.pipeline.validator")


@dataclass
class ValidationResult:
    """校验结果"""
    passed: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = True        # 是否可以通过自动操作恢复
    recovery_action: str = ""       # 建议的恢复操作


class StateValidator:
    """页面状态校验器"""

    def __init__(self, page: Page):
        self.page = page

    async def validate_page_loaded(self, timeout: int = 10000) -> ValidationResult:
        """验证页面是否已加载完成"""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            return ValidationResult(passed=True, message="页面已加载")
        except Exception as e:
            return ValidationResult(
                passed=False,
                message=f"页面加载超时: {e}",
                recoverable=True,
                recovery_action="refresh",
            )

    async def validate_element_ready(
        self,
        selector: str,
        timeout: int = 10000,
    ) -> ValidationResult:
        """验证目标元素是否已准备就绪（可见且可交互）"""
        try:
            locator = self.page.locator(selector)
            await locator.wait_for(state="visible", timeout=timeout)

            # 检查是否被其他元素遮挡
            is_enabled = await locator.is_enabled()
            if not is_enabled:
                return ValidationResult(
                    passed=False,
                    message=f"元素 {selector} 不可用(disabled)",
                    recoverable=True,
                    recovery_action="wait_and_retry",
                )

            return ValidationResult(
                passed=True,
                message=f"元素 {selector} 已就绪",
            )
        except Exception as e:
            return ValidationResult(
                passed=False,
                message=f"元素 {selector} 未找到或不可见: {e}",
                recoverable=True,
                recovery_action="scroll_and_retry",
            )

    async def check_for_modals(self) -> ValidationResult:
        """检测是否有弹窗/遮罩层阻挡操作"""
        modal_selectors = [
            ".modal.show",
            ".modal[style*='display: block']",
            "[role='dialog']",
            ".ant-modal-wrap:not([style*='display: none'])",
            ".el-dialog__wrapper:not([style*='display: none'])",
            ".overlay:not(.hidden)",
            ".loading-mask",
            ".el-loading-mask",
        ]

        for selector in modal_selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                if count > 0:
                    is_visible = await locator.first.is_visible()
                    if is_visible:
                        return ValidationResult(
                            passed=False,
                            message=f"检测到弹窗/遮罩: {selector}",
                            details={"modal_selector": selector},
                            recoverable=True,
                            recovery_action="dismiss_modal",
                        )
            except Exception:
                continue

        return ValidationResult(passed=True, message="无弹窗阻挡")

    async def validate_url(self, expected_url_pattern: str) -> ValidationResult:
        """验证当前URL是否符合预期"""
        current_url = self.page.url
        if expected_url_pattern in current_url:
            return ValidationResult(passed=True, message=f"URL匹配: {current_url}")
        return ValidationResult(
            passed=False,
            message=f"URL不匹配, 期望包含: {expected_url_pattern}, 实际: {current_url}",
            recoverable=True,
            recovery_action="navigate",
        )

    async def validate_fill_result(
        self,
        selector: str,
        expected_value: str,
    ) -> ValidationResult:
        """验证表单字段值是否正确填入"""
        try:
            locator = self.page.locator(selector)
            actual_value = await locator.input_value()
            if actual_value == expected_value:
                return ValidationResult(
                    passed=True,
                    message=f"字段值正确: {expected_value}",
                )
            return ValidationResult(
                passed=False,
                message=f"字段值不匹配, 期望: {expected_value}, 实际: {actual_value}",
                recoverable=True,
                recovery_action="clear_and_refill",
            )
        except Exception as e:
            return ValidationResult(
                passed=False,
                message=f"验证字段值失败: {e}",
                recoverable=True,
                recovery_action="retry",
            )

    async def get_page_state(self) -> dict:
        """获取当前页面状态快照"""
        try:
            return {
                "url": self.page.url,
                "title": await self.page.title(),
                "has_modal": not (await self.check_for_modals()).passed,
                "ready_state": await self.page.evaluate("document.readyState"),
            }
        except Exception as e:
            return {
                "url": "unknown",
                "title": "unknown",
                "error": str(e),
            }

    async def dismiss_modal(self) -> bool:
        """尝试关闭弹窗"""
        close_selectors = [
            ".modal .close",
            ".modal .btn-close",
            "[role='dialog'] button[aria-label='Close']",
            ".ant-modal-close",
            ".el-dialog__close",
            ".el-message-box__close",
            "button.cancel",
        ]

        for selector in close_selectors:
            try:
                locator = self.page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.click()
                    await asyncio.sleep(0.5)
                    logger.info(f"已关闭弹窗: {selector}")
                    return True
            except Exception:
                continue

        # 尝试按 Escape 键
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            modal_result = await self.check_for_modals()
            if modal_result.passed:
                logger.info("通过 Escape 键关闭弹窗")
                return True
        except Exception:
            pass

        return False
