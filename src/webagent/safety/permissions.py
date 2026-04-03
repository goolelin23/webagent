"""
权限管理
基于操作类型的权限配置和URL白名单/黑名单
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any

from webagent.utils.logger import get_logger

logger = get_logger("webagent.safety.permissions")


@dataclass
class PermissionRule:
    """权限规则"""
    name: str
    allowed_actions: list[str] = field(default_factory=lambda: ["*"])
    blocked_actions: list[str] = field(default_factory=list)
    url_whitelist: list[str] = field(default_factory=list)
    url_blacklist: list[str] = field(default_factory=list)
    max_actions_per_minute: int = 30
    require_confirmation_for: list[str] = field(default_factory=list)


class PermissionManager:
    """权限管理器"""

    def __init__(self):
        self._rules: list[PermissionRule] = [self._default_rule()]
        self._action_count: dict[str, int] = {}  # 每分钟计数
        self._current_rule: PermissionRule = self._default_rule()

    def _default_rule(self) -> PermissionRule:
        """默认权限规则"""
        return PermissionRule(
            name="default",
            allowed_actions=["*"],
            blocked_actions=[],
            url_blacklist=[
                r".*\/admin\/system.*",
                r".*\/super-admin.*",
                r".*\/database\/drop.*",
            ],
            max_actions_per_minute=30,
            require_confirmation_for=["delete", "batch_delete", "reset"],
        )

    def set_rule(self, rule: PermissionRule):
        """设置当前权限规则"""
        self._current_rule = rule
        logger.info(f"权限规则已更新: {rule.name}")

    def check_permission(self, action: str, url: str = "") -> dict[str, Any]:
        """
        检查操作权限

        Returns:
            {
                "allowed": bool,
                "reason": str,
                "requires_confirmation": bool
            }
        """
        rule = self._current_rule

        # 检查黑名单URL
        for pattern in rule.url_blacklist:
            if re.match(pattern, url, re.IGNORECASE):
                return {
                    "allowed": False,
                    "reason": f"URL在黑名单中: {pattern}",
                    "requires_confirmation": False,
                }

        # 如果有白名单，检查URL是否在白名单中
        if rule.url_whitelist:
            is_whitelisted = any(
                re.match(pattern, url, re.IGNORECASE)
                for pattern in rule.url_whitelist
            )
            if not is_whitelisted:
                return {
                    "allowed": False,
                    "reason": "URL不在白名单中",
                    "requires_confirmation": False,
                }

        # 检查操作是否被阻止
        if action in rule.blocked_actions:
            return {
                "allowed": False,
                "reason": f"操作被禁止: {action}",
                "requires_confirmation": False,
            }

        # 检查操作是否被允许
        if "*" not in rule.allowed_actions and action not in rule.allowed_actions:
            return {
                "allowed": False,
                "reason": f"操作未授权: {action}",
                "requires_confirmation": False,
            }

        # 检查是否需要确认
        requires_confirmation = action in rule.require_confirmation_for

        return {
            "allowed": True,
            "reason": "",
            "requires_confirmation": requires_confirmation,
        }

    def check_rate_limit(self) -> bool:
        """检查操作频率是否超限"""
        import time
        current_minute = int(time.time() / 60)
        key = str(current_minute)

        count = self._action_count.get(key, 0)
        if count >= self._current_rule.max_actions_per_minute:
            logger.warning(
                f"操作频率超限: {count}/{self._current_rule.max_actions_per_minute}/分钟"
            )
            return False

        self._action_count[key] = count + 1

        # 清理旧计数
        old_keys = [k for k in self._action_count if k != key]
        for k in old_keys:
            del self._action_count[k]

        return True
