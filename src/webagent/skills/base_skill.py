"""
技能基类
定义所有技能插件的统一接口
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool
    value: Any = None
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseSkill(ABC):
    """技能插件基类"""

    # 子类必须定义
    name: str = ""
    description: str = ""
    version: str = "1.0.0"

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> SkillResult:
        """
        执行技能

        Args:
            params: 技能参数字典
        Returns:
            SkillResult 执行结果
        """
        ...

    def validate_params(self, params: dict[str, Any]) -> tuple[bool, str]:
        """
        验证参数是否合法（子类可覆盖）

        Returns:
            (is_valid, error_message)
        """
        return True, ""

    def get_info(self) -> dict:
        """获取技能信息"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }

    def __repr__(self) -> str:
        return f"<Skill: {self.name} v{self.version}>"
