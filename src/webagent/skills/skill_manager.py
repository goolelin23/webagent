"""
技能插件管理器
自动发现、注册和调用技能插件
"""

from __future__ import annotations
import importlib
import pkgutil
from pathlib import Path
from typing import Any

from webagent.skills.base_skill import BaseSkill, SkillResult
from webagent.utils.logger import get_logger

logger = get_logger("webpilot.skills")


class SkillManager:
    """技能插件管理器"""

    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}
        # 自动加载内置技能
        self._load_builtin_skills()

    def _load_builtin_skills(self):
        """自动发现并加载 builtin 目录下的所有技能"""
        builtin_path = Path(__file__).parent / "builtin"
        if not builtin_path.exists():
            return

        for loader, module_name, is_pkg in pkgutil.iter_modules([str(builtin_path)]):
            if module_name.startswith("_"):
                continue
            try:
                module = importlib.import_module(
                    f"webpilot.skills.builtin.{module_name}"
                )
                # 查找模块中的 BaseSkill 子类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseSkill)
                        and attr is not BaseSkill
                        and attr.name  # 必须定义 name
                    ):
                        skill_instance = attr()
                        self.register(skill_instance)
                        logger.debug(f"加载内置技能: {skill_instance.name}")
            except Exception as e:
                logger.warning(f"加载技能模块失败 [{module_name}]: {e}")

    def register(self, skill: BaseSkill):
        """
        注册技能插件

        Args:
            skill: BaseSkill 实例
        """
        if skill.name in self._skills:
            logger.warning(f"技能 [{skill.name}] 已存在，将被覆盖")
        self._skills[skill.name] = skill
        logger.info(f"注册技能: {skill.name} — {skill.description}")

    def unregister(self, name: str):
        """注销技能"""
        if name in self._skills:
            del self._skills[name]
            logger.info(f"注销技能: {name}")

    async def execute_skill(
        self,
        name: str,
        params: dict[str, Any],
    ) -> SkillResult:
        """
        执行指定技能

        Args:
            name: 技能名称
            params: 参数字典
        Returns:
            SkillResult
        """
        skill = self._skills.get(name)
        if not skill:
            return SkillResult(
                success=False,
                message=f"技能不存在: {name}",
            )

        # 验证参数
        is_valid, error_msg = skill.validate_params(params)
        if not is_valid:
            return SkillResult(
                success=False,
                message=f"参数验证失败: {error_msg}",
            )

        try:
            result = await skill.execute(params)
            logger.info(f"技能 [{name}] 执行成功: {result.value}")
            return result
        except Exception as e:
            logger.error(f"技能 [{name}] 执行异常: {e}")
            return SkillResult(
                success=False,
                message=f"执行异常: {str(e)}",
            )

    def list_skills(self) -> list[dict]:
        """列出所有已注册的技能"""
        return [skill.get_info() for skill in self._skills.values()]

    def get_skills_prompt(self) -> str:
        """生成技能列表的提示词文本"""
        if not self._skills:
            return "（无可用技能插件）"

        lines = ["可用技能插件:"]
        for skill in self._skills.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def has_skill(self, name: str) -> bool:
        """检查技能是否已注册"""
        return name in self._skills
