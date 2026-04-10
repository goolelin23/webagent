"""
OpenClaw (龙虾) 技能导出器
将 WebPilot AI 的知识库和页面技能导出为 OpenClaw 兼容的 SKILL.md 格式
支持 OpenClaw 直接加载使用
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime

from webagent.knowledge.models import (
    SiteKnowledge, PageSkillDef, WorkflowDef, DeepAnalysis,
)
from webagent.knowledge.store import KnowledgeStore
from webagent.utils.logger import get_logger, print_agent, print_success

logger = get_logger("webpilot.openclaw")


class OpenClawExporter:
    """
    OpenClaw 技能导出器

    将 WebPilot AI 扫描生成的知识库导出为 OpenClaw 的 Skill 格式:
    - 每个页面技能 → 一个 SKILL.md
    - 每个业务流程 → 一个 SKILL.md
    - 站点总览 → 一个顶层 SKILL.md
    """

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or Path("openclaw_skills")
        self.knowledge_store = KnowledgeStore()

    def export_site(self, domain: str) -> Path:
        """
        导出整个站点的知识为 OpenClaw 技能包

        Args:
            domain: 站点域名
        Returns:
            导出目录路径
        """
        site = self.knowledge_store.load(domain)
        if not site:
            raise ValueError(f"未找到站点知识库: {domain}")

        analysis = site.get_deep_analysis()

        # 创建导出目录
        safe_domain = domain.replace(":", "_").replace(".", "_").replace("/", "_")
        export_dir = self.output_dir / safe_domain
        export_dir.mkdir(parents=True, exist_ok=True)

        print_agent("openclaw", f"🦞 开始导出 OpenClaw 技能包: {domain}")

        exported_count = 0

        # 1. 导出站点总览技能
        self._export_site_overview(site, analysis, export_dir)
        exported_count += 1

        # 2. 导出每个页面技能
        if analysis:
            for url, skill_dicts in analysis.page_skills.items():
                for sd in skill_dicts:
                    skill = PageSkillDef.from_dict(sd)
                    self._export_page_skill(site, skill, export_dir)
                    exported_count += 1

            # 3. 导出业务流程技能
            for wf in analysis.get_workflows():
                self._export_workflow(site, wf, analysis, export_dir)
                exported_count += 1

        # 4. 导出知识库 JSON（供技能引用）
        knowledge_file = export_dir / "knowledge_base.json"
        knowledge_file.write_text(site.to_json(), encoding="utf-8")

        print_success(f"🦞 OpenClaw 技能包导出完成! {exported_count} 个技能 → {export_dir}")
        return export_dir

    def _export_site_overview(
        self,
        site: SiteKnowledge,
        analysis: DeepAnalysis | None,
        export_dir: Path,
    ):
        """导出站点总览技能"""
        skill_dir = export_dir / "site_overview"
        skill_dir.mkdir(exist_ok=True)

        # 构建页面列表
        pages_info = []
        for url, page in site.pages.items():
            pages_info.append(f"- **{page.title or '未命名'}** ({page.page_type or 'unknown'}): `{url}`")

        # 构建技能列表
        skills_info = ""
        workflows_info = ""
        entities_info = ""
        if analysis:
            skills = analysis.get_all_skills()
            if skills:
                skills_lines = []
                for s in skills:
                    params = ", ".join(s.get_param_names())
                    skills_lines.append(f"- `{s.skill_id}({params})` — {s.name}: {s.description}")
                skills_info = "\n".join(skills_lines)

            wfs = analysis.get_workflows()
            if wfs:
                wf_lines = []
                for wf in wfs:
                    keywords = ", ".join(wf.trigger_keywords[:5])
                    wf_lines.append(f"- **{wf.name}** (关键词: {keywords})")
                    wf_lines.append(f"  步骤: {' → '.join(wf.skill_sequence)}")
                workflows_info = "\n".join(wf_lines)

            if analysis.business_entities:
                entities_info = ", ".join(analysis.business_entities)

        content = f"""---
name: webpilot_{site.domain.replace('.', '_').replace(':', '_')}_overview
description: "WebPilot AI 站点知识总览 — {site.site_name or site.domain}。了解该系统的页面结构、可用操作和业务流程。"
user-invocable: true
metadata:
  openclaw.requires.bins: []
  openclaw.os: ["darwin", "linux", "windows"]
  webpilot.domain: "{site.domain}"
  webpilot.version: "2.0"
---

# 🤖 WebPilot AI 站点知识 — {site.site_name or site.domain}

你已经学习了关于 **{site.site_name or site.domain}** 的完整知识。当用户要求你操作该系统时，请使用以下知识。

## 系统描述
{analysis.system_description if analysis else '暂无描述，请先执行扫描分析。'}

## 业务实体
{entities_info or '暂无'}

## 页面清单 ({len(site.pages)} 个页面)
{chr(10).join(pages_info) if pages_info else '暂无页面数据'}

## 可用的页面操作技能
{skills_info or '暂无技能，请先执行深度分析。'}

## 已识别的业务流程
{workflows_info or '暂无流程'}

## 使用说明

### 当用户给出指令时：
1. **匹配流程**：检查指令是否匹配上述"已识别的业务流程"的关键词
2. **查找技能**：找到对应的页面操作技能
3. **组装步骤**：按技能的步骤序列执行浏览器操作
4. **参数填充**：将用户提供的值填入技能参数

### 浏览器操作方式：
- 使用 Chrome DevTools Protocol 控制浏览器
- 导航到目标页面 URL
- 按照技能步骤执行 fill / click / select / wait 等操作

### 知识库文件
- 完整知识库数据位于同目录的 `knowledge_base.json`
- 如需查看具体元素选择器，请读取该文件
"""
        (skill_dir / "SKILL.md").write_text(content.strip(), encoding="utf-8")

    def _export_page_skill(
        self,
        site: SiteKnowledge,
        skill: PageSkillDef,
        export_dir: Path,
    ):
        """导出单个页面技能"""
        skill_dir = export_dir / f"skill_{skill.skill_id}"
        skill_dir.mkdir(exist_ok=True)

        page = site.get_page(skill.page_url)
        page_title = page.title if page else skill.page_url

        # 构建参数文档
        params_doc = ""
        if skill.parameters:
            param_lines = []
            for p in skill.parameters:
                req = "必填" if p.get("required") else "可选"
                opts = f" (选项: {', '.join(p['options'])})" if p.get("options") else ""
                label = p.get("label", p["name"])
                param_lines.append(f"- **{label}** (`{p['name']}`): {p['type']} [{req}]{opts}")
                if p.get("selector"):
                    param_lines.append(f"  - CSS选择器: `{p['selector']}`")
            params_doc = "\n".join(param_lines)

        # 构建执行步骤
        steps_doc = ""
        if skill.steps:
            step_lines = []
            for i, step in enumerate(skill.steps, 1):
                action = step.get("action", "")
                target = step.get("target", "")
                value = step.get("value", "")
                desc = step.get("description", "")
                step_line = f"{i}. **{desc}**"
                if action:
                    step_line += f"\n   - 操作: `{action}`"
                if target:
                    step_line += f"\n   - 目标: `{target}`"
                if value:
                    step_line += f"\n   - 值: `{value}`"
                step_lines.append(step_line)
            steps_doc = "\n".join(step_lines)

        content = f"""---
name: webpilot_{skill.skill_id}
description: "{skill.name} — {skill.description}"
user-invocable: false
metadata:
  openclaw.requires.bins: []
  webpilot.skill_type: "{skill.skill_type}"
  webpilot.page_url: "{skill.page_url}"
  webpilot.auto_generated: true
---

# 🔧 {skill.name}

**类型**: {skill.skill_type}
**页面**: {page_title} (`{skill.page_url}`)

## 描述
{skill.description}

## 参数
{params_doc or '无需参数'}

## 前置条件
{chr(10).join('- ' + c for c in skill.preconditions) if skill.preconditions else '- 无'}

## 执行步骤
{steps_doc or '暂无步骤'}

## 执行后效果
{chr(10).join('- ' + c for c in skill.postconditions) if skill.postconditions else '- 无'}

## 浏览器操作指令

当需要执行此技能时，请按以下步骤操作浏览器：

```
页面: {skill.page_url}
"""
        # 追加步骤的简洁版
        for step in skill.steps:
            action = step.get("action", "")
            target = step.get("target", "")
            value = step.get("value", "")
            content += f"{action.upper()}: target=\"{target}\" value=\"{value}\"\n"

        content += "```\n"

        (skill_dir / "SKILL.md").write_text(content.strip(), encoding="utf-8")

        # 保存原始技能定义 JSON（供程序化使用）
        (skill_dir / "skill_def.json").write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _export_workflow(
        self,
        site: SiteKnowledge,
        wf: WorkflowDef,
        analysis: DeepAnalysis,
        export_dir: Path,
    ):
        """导出业务流程技能"""
        skill_dir = export_dir / f"workflow_{wf.workflow_id}"
        skill_dir.mkdir(exist_ok=True)

        # 构建技能序列详情
        sequence_doc = []
        for skill_id in wf.skill_sequence:
            # 查找对应的技能定义
            matched_skill = None
            for url, skill_list in analysis.page_skills.items():
                for sd in skill_list:
                    if sd.get("skill_id") == skill_id:
                        matched_skill = PageSkillDef.from_dict(sd)
                        break
                if matched_skill:
                    break

            if matched_skill:
                params = ", ".join(matched_skill.get_param_names())
                sequence_doc.append(
                    f"### 步骤: {matched_skill.name}\n"
                    f"- 技能: `{skill_id}({params})`\n"
                    f"- 页面: `{matched_skill.page_url}`\n"
                    f"- 说明: {matched_skill.description}"
                )
            else:
                sequence_doc.append(f"### 步骤: {skill_id}\n- (未找到详细定义)")

        keywords_str = ", ".join(wf.trigger_keywords)

        content = f"""---
name: webpilot_workflow_{wf.workflow_id}
description: "{wf.name} — {wf.description or '完整业务流程'}。触发关键词: {keywords_str}"
user-invocable: true
metadata:
  openclaw.requires.bins: []
  webpilot.workflow: true
  webpilot.trigger_keywords: {json.dumps(wf.trigger_keywords, ensure_ascii=False)}
---

# 🔄 {wf.name}

**触发关键词**: {keywords_str}

## 描述
{wf.description or wf.name}

## 前置条件
{chr(10).join('- ' + c for c in wf.preconditions) if wf.preconditions else '- 无'}

## 流程步骤

{chr(10).join(sequence_doc)}

## 预期结果
{wf.expected_outcome or '流程执行完成'}

## 使用说明

当用户的指令包含以下任意关键词时，执行此流程:
**{keywords_str}**

按照上述步骤序列依次执行每个技能。每个技能的具体操作步骤请参考对应的技能 SKILL.md。
"""
        (skill_dir / "SKILL.md").write_text(content.strip(), encoding="utf-8")

        # 保存工作流定义 JSON
        (skill_dir / "workflow_def.json").write_text(
            json.dumps(wf.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def export_for_openclaw(domain: str, output_dir: str | None = None) -> Path:
    """
    便捷函数: 导出站点知识为 OpenClaw 技能包

    Args:
        domain: 站点域名
        output_dir: 输出目录（默认 ./openclaw_skills）
    Returns:
        导出目录路径
    """
    exporter = OpenClawExporter(
        output_dir=Path(output_dir) if output_dir else None,
    )
    return exporter.export_site(domain)
