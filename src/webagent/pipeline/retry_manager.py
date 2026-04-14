"""
重试机制管理器
支持固定间隔重试、指数退避，以及操作前状态恢复
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any
from webagent.utils.logger import get_logger
from webagent.utils.config import get_config

logger = get_logger("webpilot.pipeline.retry")


@dataclass
class RetryResult:
    """重试结果"""
    success: bool
    result: Any = None
    attempts: int = 0
    last_error: str = ""
    should_replan: bool = False     # 是否需要重新规划


@dataclass
class RetryPolicy:
    """重试策略"""
    max_retries: int = 5
    base_delay: float = 2.0        # 基础延迟(秒)
    max_delay: float = 30.0         # 最大延迟(秒)
    exponential: bool = True        # 是否使用指数退避
    retry_on_exceptions: tuple = (Exception,)


class RetryManager:
    """重试机制管理器"""

    def __init__(self, policy: RetryPolicy | None = None):
        config = get_config()
        self.policy = policy or RetryPolicy(
            max_retries=config.pipeline.max_retries,
            base_delay=config.pipeline.retry_delay,
        )
        self._recovery_handlers: dict[str, Callable] = {}

    def register_recovery(self, action: str, handler: Callable):
        """
        注册恢复操作处理器

        Args:
            action: 恢复操作名称（如 dismiss_modal, refresh, scroll_and_retry 等）
            handler: 异步恢复函数
        """
        self._recovery_handlers[action] = handler
        logger.debug(f"注册恢复处理器: {action}")

    async def execute_with_retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        recovery_action: str = "",
        description: str = "",
    ) -> RetryResult:
        """
        带重试机制执行操作

        Args:
            operation: 要执行的异步操作
            recovery_action: 重试前执行的恢复操作名称
            description: 操作描述（用于日志）
        Returns:
            RetryResult
        """
        last_error = ""

        for attempt in range(1, self.policy.max_retries + 1):
            try:
                result = await operation()
                if attempt > 1:
                    logger.info(
                        f"第{attempt}次重试成功: {description}"
                    )
                return RetryResult(
                    success=True,
                    result=result,
                    attempts=attempt,
                )
            except self.policy.retry_on_exceptions as e:
                last_error = str(e)
                logger.warning(
                    f"操作失败 (第{attempt}/{self.policy.max_retries}次): "
                    f"{description} | 错误: {last_error}"
                )

                if attempt < self.policy.max_retries:
                    # 执行恢复操作
                    if recovery_action and recovery_action in self._recovery_handlers:
                        try:
                            logger.info(f"执行恢复操作: {recovery_action}")
                            await self._recovery_handlers[recovery_action]()
                        except Exception as re:
                            logger.warning(f"恢复操作失败: {re}")

                    # 等待延迟
                    delay = self._calculate_delay(attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

        # 所有重试都失败
        logger.error(
            f"操作失败, 已达到最大重试次数({self.policy.max_retries}): {description}"
        )
        return RetryResult(
            success=False,
            attempts=self.policy.max_retries,
            last_error=last_error,
            should_replan=True,  # 建议重新规划路径
        )

    def _calculate_delay(self, attempt: int) -> float:
        """计算重试延迟"""
        if self.policy.exponential:
            delay = self.policy.base_delay * (2 ** (attempt - 1))
        else:
            delay = self.policy.base_delay
        return min(delay, self.policy.max_delay)

    async def execute_with_fallback(
        self,
        primary: Callable[[], Awaitable[Any]],
        fallbacks: list[Callable[[], Awaitable[Any]]],
        description: str = "",
    ) -> RetryResult:
        """
        带降级方案的执行（先尝试主操作，失败后依次尝试备选方案）

        Args:
            primary: 主操作
            fallbacks: 备选操作列表
            description: 操作描述
        """
        # 先尝试主操作
        result = await self.execute_with_retry(primary, description=description)
        if result.success:
            return result

        # 逐一尝试备选方案
        for i, fallback in enumerate(fallbacks):
            logger.info(f"尝试备选方案 {i + 1}/{len(fallbacks)}: {description}")
            try:
                fb_result = await fallback()
                return RetryResult(
                    success=True,
                    result=fb_result,
                    attempts=result.attempts + i + 1,
                )
            except Exception as e:
                logger.warning(f"备选方案 {i + 1} 失败: {e}")
                continue

        return RetryResult(
            success=False,
            attempts=result.attempts + len(fallbacks),
            last_error=f"所有方案均失败: {result.last_error}",
            should_replan=True,
        )
