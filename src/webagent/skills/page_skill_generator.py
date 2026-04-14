"""
页面技能自动生成器
将 DeepAnalysis 中的 PageSkillDef 转化为可执行的 BaseSkill 实例
"""

from __future__ import annotations
from typing import Any

from webagent.knowledge.models import (
    DeepAnalysis, PageSkillDef, SiteKnowledge,
)
from webagent.skills.base_skill import BaseSkill, SkillResult
from webagent.skills.skill_manager import SkillManager
from webagent.utils.logger import get_logger, print_agent

logger = get_logger("webagent.skills.page_skill_generator")


class DynamicPageSkill(BaseSkill):
    """
    动态页面技能 — 基于 PageSkillDef 自动生成的可执行技能
    """

    def __init__(self, skill_def: PageSkillDef):
        self.skill_def = skill_def
        self.name = skill_def.skill_id
        self.description = f"{skill_def.name}: {skill_def.description}"
        self.version = "auto"

    def validate_params(self, params: dict[str, Any]) -> tuple[bool, str]:
        """验证必填参数"""
        for p in self.skill_def.get_required_params():
            if p["name"] not in params:
                return False, f"缺少必填参数: {p['name']} ({p.get('label', '')})"
        return True, ""

    async def execute(self, params: dict[str, Any]) -> SkillResult:
        """
        执行页面技能 — 返回渲染后的执行步骤
        实际的浏览器操作由 Pipeline 执行，这里只做参数填充
        """
        try:
            rendered_steps = self._render_steps(params)
            return SkillResult(
                success=True,
                value=rendered_steps,
                message=f"技能 [{self.name}] 已生成 {len(rendered_steps)} 个步骤",
                metadata={
                    "skill_id": self.skill_def.skill_id,
                    "skill_type": self.skill_def.skill_type,
                    "page_url": self.skill_def.page_url,
                    "steps": rendered_steps,
                },
            )
        except Exception as e:
            return SkillResult(
                success=False,
                message=f"技能执行失败: {e}",
            )

    def _render_steps(self, params: dict[str, Any]) -> list[dict]:
        """
        将 PageSkillDef.steps 中的占位符替换为实际参数值

        例如:  {username} → 实际的用户名
        """
        rendered = []
        for step in self.skill_def.steps:
            new_step = dict(step)

            # 替换 value 中的参数占位符
            if "value" in new_step and new_step["value"]:
                value = new_step["value"]
                for param_name, param_value in params.items():
                    value = value.replace(f"{{{param_name}}}", str(param_value))
                new_step["value"] = value

            # 替换 target 中的参数占位符
            if "target" in new_step and new_step["target"]:
                target = new_step["target"]
                for param_name, param_value in params.items():
                    target = target.replace(f"{{{param_name}}}", str(param_value))
                new_step["target"] = target

            rendered.append(new_step)

        return rendered

    def get_info(self) -> dict:
        """获取技能信息"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "type": self.skill_def.skill_type,
            "page_url": self.skill_def.page_url,
            "parameters": self.skill_def.parameters,
            "auto_generated": True,
        }


class PageSkillGenerator:
    """
    页面技能生成器 — 从 DeepAnalysis 批量生成可执行技能
    """

    def __init__(self, skill_manager: SkillManager):
        self.skill_manager = skill_manager

    def generate_and_register(self, site: SiteKnowledge) -> int:
        """
        从站点知识库的深度分析结果中生成并注册所有页面技能

        Args:
            site: 包含 deep_analysis 的站点知识
        Returns:
            注册的技能数量
        """
        analysis = site.get_deep_analysis()
        if not analysis:
            logger.warning(f"站点 [{site.domain}] 暂无深度分析结果")
            return 0

        count = 0
        all_skills = analysis.get_all_skills()

        for skill_def in all_skills:
            try:
                dynamic_skill = DynamicPageSkill(skill_def)
                self.skill_manager.register(dynamic_skill)
                count += 1
            except Exception as e:
                logger.warning(f"注册页面技能失败 [{skill_def.skill_id}]: {e}")

        print_agent("explorer", f"已注册 {count} 个页面技能")
        return count

    def generate_from_analysis(self, analysis: DeepAnalysis) -> list[DynamicPageSkill]:
        """从 DeepAnalysis 生成技能实例列表（不注册）"""
        skills = []
        for skill_def in analysis.get_all_skills():
            try:
                skills.append(DynamicPageSkill(skill_def))
            except Exception as e:
                logger.warning(f"生成页面技能失败 [{skill_def.skill_id}]: {e}")
        return skills

    def get_skills_summary(self, site: SiteKnowledge) -> str:
        """获取页面技能摘要"""
        analysis = site.get_deep_analysis()
        if not analysis:
            return "（暂无页面技能，请先执行扫描和深度分析）"

        lines = [f"📦 站点 [{site.domain}] 的页面技能:\n"]

        for url, skill_dicts in analysis.page_skills.items():
            page = site.get_page(url)
            page_title = page.title if page else url
            lines.append(f"  📄 {page_title}")

            for sd in skill_dicts:
                skill = PageSkillDef.from_dict(sd)
                params = ", ".join(skill.get_param_names())
                lines.append(f"    • {skill.skill_id}({params}) — {skill.name}")

            lines.append("")

        # 工作流
        workflows = analysis.get_workflows()
        if workflows:
            lines.append("🔄 业务流程:")
            for wf in workflows:
                keywords = ", ".join(wf.trigger_keywords[:3])
                lines.append(f"  • {wf.name} [关键词: {keywords}]")
                lines.append(f"    步骤: {' → '.join(wf.skill_sequence)}")

        return "\n".join(lines)
