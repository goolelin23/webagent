"""
深度分析器
扫描完知识库后，调用 LLM 深度分析页面语义、生成页面技能和业务流程
"""

from __future__ import annotations
import json
import re
from typing import Any

from webagent.knowledge.models import (
    SiteKnowledge, PageKnowledge, DeepAnalysis,
    PageSkillDef, WorkflowDef, FormInfo,
)
from webagent.utils.logger import get_logger, print_agent, print_success, console
from webagent.utils.llm import get_llm

logger = get_logger("webagent.knowledge.deep_analyzer")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM 分析提示词
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PAGE_ANALYSIS_PROMPT = """请分析以下Web页面，识别该页面上所有可执行的原子操作（技能）。

## 页面信息：
- URL: {url}
- 标题: {title}
- 页面类型: {page_type}

## 页面元素：
{elements_desc}

## 表单结构：
{forms_desc}

## 导航链接：
{nav_desc}

请以JSON格式返回该页面的技能列表，每个技能代表一个用户可以执行的原子操作：

```json
{{
    "page_description": "这个页面的功能简述",
    "skills": [
        {{
            "skill_id": "技能唯一ID（英文下划线，如 login, create_order）",
            "name": "技能名称（中文）",
            "description": "技能的详细描述",
            "skill_type": "类型: login | form_fill | search | navigation | crud_create | crud_delete | action",
            "parameters": [
                {{
                    "name": "参数名",
                    "type": "text | select | number | date | checkbox",
                    "required": true,
                    "selector": "CSS选择器",
                    "label": "字段标签",
                    "options": ["选项1", "选项2"]
                }}
            ],
            "steps": [
                {{
                    "action": "navigate | click | fill | select | wait | assert",
                    "target": "CSS选择器或URL",
                    "value": "填入的值(可用参数占位符如 {{username}})",
                    "description": "步骤说明"
                }}
            ],
            "preconditions": ["前置条件"],
            "postconditions": ["执行后的效果"]
        }}
    ]
}}
```

注意：
1. 每个可点击的按钮/链接都可能是一个技能
2. 表单的填写+提交是一个完整技能
3. 搜索框+搜索按钮是一个搜索技能
4. 技能的 steps 中用 {{参数名}} 作为参数占位符
5. 只分析当前页面能执行的操作，不要猜测其他页面"""

WORKFLOW_ANALYSIS_PROMPT = """请分析以下Web系统的页面结构，识别完整的业务流程。

## 系统概况：
- 站点: {site_name}
- 页面总数: {total_pages}

## 所有页面及其技能：
{pages_and_skills}

## 页面间的导航关系：
{navigation_map}

请以JSON格式返回：

```json
{{
    "system_description": "对这个系统的整体描述（它是做什么的）",
    "business_entities": ["业务实体1", "业务实体2"],
    "workflows": [
        {{
            "workflow_id": "流程唯一ID（英文下划线）",
            "name": "流程名称（中文）",
            "description": "流程详细描述",
            "trigger_keywords": ["触发关键词1", "关键词2", "关键词3"],
            "skill_sequence": ["技能ID1", "技能ID2", "技能ID3"],
            "pages_involved": ["页面URL1", "页面URL2"],
            "preconditions": ["需要已登录"],
            "expected_outcome": "流程完成后的结果"
        }}
    ],
    "page_relationships": {{
        "页面URL1": ["可到达的页面URL1", "可到达的页面URL2"]
    }}
}}
```

注意：
1. trigger_keywords 要尽量多列，覆盖用户可能的自然语言表达
2. 一个完整的业务流程往往跨越多个页面
3. 每个流程的 skill_sequence 引用的是各个页面技能的 skill_id
4. 常见流程模式：登录→导航→填表→提交，登录→搜索→查看详情"""


class DeepAnalyzer:
    """
    深度分析器 — 扫描后 LLM 深度理解系统

    工作流程:
    1. 逐页分析: 提取每个页面的原子操作技能
    2. 全局分析: 识别跨页面的业务流程和页面关系
    3. 汇总: 组装 DeepAnalysis 结果
    """

    def __init__(self):
        self.llm = None  # 延迟初始化

    def _get_llm(self):
        if self.llm is None:
            self.llm = get_llm()
        return self.llm

    async def analyze(self, site: SiteKnowledge) -> DeepAnalysis:
        """
        对站点知识进行深度分析

        Args:
            site: 已扫描的站点知识
        Returns:
            DeepAnalysis 深度分析结果
        """
        print_agent("explorer", f"🧠 开始深度分析: {site.domain}")
        print_agent("explorer", f"   待分析页面: {len(site.pages)}")

        # 阶段1: 逐页分析，生成页面技能
        all_page_skills: dict[str, list[dict]] = {}
        for i, (url, page) in enumerate(site.pages.items()):
            print_agent("explorer", f"   分析页面 [{i+1}/{len(site.pages)}]: {page.title or url}")
            try:
                skills = await self._analyze_page(page)
                if skills:
                    all_page_skills[url] = [s.to_dict() for s in skills]
                    print_agent("explorer", f"     → 发现 {len(skills)} 个技能")
            except Exception as e:
                logger.warning(f"页面分析失败 [{url}]: {e}")

        # 阶段2: 全局分析，识别业务流程
        print_agent("explorer", "   分析业务流程和页面关系...")
        try:
            global_result = await self._analyze_workflows(site, all_page_skills)
        except Exception as e:
            logger.warning(f"工作流分析失败: {e}")
            global_result = {
                "system_description": "",
                "business_entities": [],
                "workflows": [],
                "page_relationships": {},
            }

        # 阶段3: 汇总结果
        analysis = DeepAnalysis(
            page_skills=all_page_skills,
            workflows=global_result.get("workflows", []),
            page_relationships=global_result.get("page_relationships", {}),
            business_entities=global_result.get("business_entities", []),
            system_description=global_result.get("system_description", ""),
        )

        total_skills = len(analysis.get_all_skills())
        total_workflows = len(analysis.get_workflows())
        print_success(
            f"深度分析完成! 生成 {total_skills} 个页面技能, "
            f"{total_workflows} 个业务流程"
        )

        return analysis

    async def analyze_single_page(self, page: PageKnowledge) -> list[PageSkillDef]:
        """单页面分析（供增量分析使用）"""
        return await self._analyze_page(page)

    async def _analyze_page(self, page: PageKnowledge) -> list[PageSkillDef]:
        """对单个页面进行 LLM 分析，生成页面技能"""

        # 如果页面太简单，使用规则生成（不调用 LLM）
        rule_skills = self._rule_based_page_skills(page)
        if not page.elements and not page.forms:
            return rule_skills

        # 构建页面描述
        elements_desc = self._describe_elements(page)
        forms_desc = self._describe_forms(page)
        nav_desc = self._describe_navigation(page)

        prompt = PAGE_ANALYSIS_PROMPT.format(
            url=page.url,
            title=page.title or "未知",
            page_type=page.page_type or "未知",
            elements_desc=elements_desc or "（无交互元素）",
            forms_desc=forms_desc or "（无表单）",
            nav_desc=nav_desc or "（无导航链接）",
        )

        try:
            response = await self._call_llm(prompt)
            data = self._extract_json(response)

            if data and "skills" in data:
                skills = []
                for s in data["skills"]:
                    s["page_url"] = page.url
                    skills.append(PageSkillDef.from_dict(s))
                return skills
        except Exception as e:
            logger.warning(f"LLM 页面分析失败, 使用规则生成: {e}")

        return rule_skills

    async def _analyze_workflows(
        self,
        site: SiteKnowledge,
        page_skills: dict[str, list[dict]],
    ) -> dict:
        """全局分析：识别业务流程"""

        # 构建页面和技能描述
        pages_and_skills = []
        for url, page in site.pages.items():
            skills = page_skills.get(url, [])
            skill_names = [s.get("name", s.get("skill_id", "")) for s in skills]
            pages_and_skills.append(
                f"  页面: {page.title or url} ({page.page_type or '?'})\n"
                f"  URL: {url}\n"
                f"  技能: {', '.join(skill_names) if skill_names else '（无）'}"
            )

        # 构建导航关系
        nav_map = []
        for url, page in site.pages.items():
            targets = [n.url for n in page.navigation if n.url]
            if targets:
                nav_map.append(f"  {page.title or url} → {', '.join(targets[:10])}")

        prompt = WORKFLOW_ANALYSIS_PROMPT.format(
            site_name=site.site_name or site.domain,
            total_pages=len(site.pages),
            pages_and_skills="\n\n".join(pages_and_skills) or "（暂无页面信息）",
            navigation_map="\n".join(nav_map) or "（暂无导航关系）",
        )

        try:
            response = await self._call_llm(prompt)
            data = self._extract_json(response)
            return data or {}
        except Exception as e:
            logger.warning(f"工作流分析失败: {e}")
            return {}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 规则引擎（不调用 LLM 的快速技能生成）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _rule_based_page_skills(self, page: PageKnowledge) -> list[PageSkillDef]:
        """基于规则快速生成页面技能（不需要 LLM）"""
        skills = []
        url = page.url
        page_type = page.page_type or ""

        # 1. 登录页面 → 生成 login 技能
        if page_type == "login" or "login" in url.lower():
            skills.append(self._gen_login_skill(page))

        # 2. 表单页面 → 生成 form_fill 技能
        for form in page.forms:
            if form.fields:
                skills.append(self._gen_form_skill(page, form))

        # 3. 有搜索框 → 生成 search 技能
        search_elements = [
            e for e in page.elements
            if e.element_type in ("search", "text")
            and any(kw in (e.placeholder or "").lower() for kw in ("搜索", "search", "查询", "查找"))
        ]
        if search_elements:
            skills.append(self._gen_search_skill(page, search_elements[0]))

        # 4. 按钮 → 生成 action 技能
        for elem in page.elements:
            if elem.tag == "button" and elem.text and elem.selector:
                text = elem.text.strip()
                if text and len(text) <= 20 and text not in ("搜索", "查询"):
                    skills.append(PageSkillDef(
                        skill_id=f"click_{elem.id or elem.name or text}".replace(" ", "_").lower(),
                        page_url=url,
                        name=f"点击「{text}」",
                        description=f"在{page.title or '当前页面'}点击「{text}」按钮",
                        skill_type="action",
                        steps=[{
                            "action": "click",
                            "target": elem.selector,
                            "description": f"点击「{text}」",
                        }],
                    ))

        return skills

    def _gen_login_skill(self, page: PageKnowledge) -> PageSkillDef:
        """生成登录技能"""
        username_selector = ""
        password_selector = ""
        submit_selector = ""

        for elem in page.elements:
            if elem.element_type == "text" and not username_selector:
                username_selector = elem.selector
            elif elem.element_type == "password":
                password_selector = elem.selector
            elif elem.element_type == "submit" or (elem.tag == "button" and "登录" in (elem.text or "")):
                submit_selector = elem.selector

        for form in page.forms:
            for f in form.fields:
                if f.field_type == "text" and not username_selector:
                    username_selector = f.selector
                elif f.field_type == "password" and not password_selector:
                    password_selector = f.selector
            if form.submit_button and not submit_selector:
                submit_selector = form.submit_button

        return PageSkillDef(
            skill_id="login",
            page_url=page.url,
            name="登录系统",
            description="输入用户名和密码登录系统",
            skill_type="login",
            parameters=[
                {"name": "username", "type": "text", "required": True, "selector": username_selector, "label": "用户名"},
                {"name": "password", "type": "password", "required": True, "selector": password_selector, "label": "密码"},
            ],
            steps=[
                {"action": "navigate", "target": page.url, "description": "打开登录页"},
                {"action": "fill", "target": username_selector, "value": "{username}", "description": "输入用户名"},
                {"action": "fill", "target": password_selector, "value": "{password}", "description": "输入密码"},
                {"action": "click", "target": submit_selector, "description": "点击登录"},
                {"action": "wait", "target": "", "value": "2000", "description": "等待登录完成"},
            ],
            preconditions=["未登录状态"],
            postconditions=["已登录，跳转到首页或仪表盘"],
        )

    def _gen_form_skill(self, page: PageKnowledge, form: FormInfo) -> PageSkillDef:
        """生成表单填写技能"""
        parameters = []
        steps = [{"action": "navigate", "target": page.url, "description": f"打开{page.title or '表单页面'}"}]

        for f in form.fields:
            param = {
                "name": f.name or f.label.replace(" ", "_").lower(),
                "type": f.field_type,
                "required": f.required,
                "selector": f.selector,
                "label": f.label or f.name,
            }
            if f.options:
                param["options"] = f.options
            parameters.append(param)

            param_name = param["name"]
            if f.field_type == "select":
                steps.append({"action": "select", "target": f.selector, "value": f"{{{param_name}}}", "description": f"选择{f.label or f.name}"})
            elif f.field_type == "checkbox":
                steps.append({"action": "check", "target": f.selector, "description": f"勾选{f.label or f.name}"})
            else:
                steps.append({"action": "fill", "target": f.selector, "value": f"{{{param_name}}}", "description": f"填写{f.label or f.name}"})

        if form.submit_button:
            steps.append({"action": "click", "target": form.submit_button, "description": "提交表单"})

        form_name = form.title or page.title or "表单"
        skill_id = f"fill_{form.form_id}".replace("-", "_").lower()

        return PageSkillDef(
            skill_id=skill_id,
            page_url=page.url,
            name=f"填写{form_name}",
            description=f"填写并提交{form_name}，包含 {len(parameters)} 个字段",
            skill_type="form_fill",
            parameters=parameters,
            steps=steps,
            postconditions=["表单已提交"],
        )

    def _gen_search_skill(self, page: PageKnowledge, search_elem) -> PageSkillDef:
        """生成搜索技能"""
        return PageSkillDef(
            skill_id=f"search_{page.page_type or 'page'}",
            page_url=page.url,
            name=f"搜索{page.title or '页面'}",
            description=f"在{page.title or '当前页面'}中搜索内容",
            skill_type="search",
            parameters=[
                {"name": "keyword", "type": "text", "required": True, "selector": search_elem.selector, "label": "搜索关键词"},
            ],
            steps=[
                {"action": "fill", "target": search_elem.selector, "value": "{keyword}", "description": "输入搜索关键词"},
                {"action": "press", "target": "", "value": "Enter", "description": "按回车搜索"},
            ],
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 工具方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _describe_elements(self, page: PageKnowledge) -> str:
        """将页面元素描述为文本"""
        lines = []
        for e in page.elements[:30]:  # 限制数量
            parts = [f"<{e.tag}"]
            if e.element_type:
                parts.append(f"type={e.element_type}")
            if e.text:
                parts.append(f"text=\"{e.text[:50]}\"")
            if e.placeholder:
                parts.append(f"placeholder=\"{e.placeholder}\"")
            if e.selector:
                parts.append(f"selector=\"{e.selector}\"")
            if e.id:
                parts.append(f"id=\"{e.id}\"")
            parts.append(f"visible={e.is_visible}")
            lines.append(" ".join(parts) + ">")
        return "\n".join(lines)

    def _describe_forms(self, page: PageKnowledge) -> str:
        """将表单描述为文本"""
        lines = []
        for form in page.forms:
            lines.append(f"表单 [{form.form_id}] action={form.action} method={form.method}")
            for f in form.fields:
                req = " (必填)" if f.required else ""
                opts = f" 选项: {f.options}" if f.options else ""
                lines.append(f"  字段: {f.label or f.name} [{f.field_type}] selector={f.selector}{req}{opts}")
            if form.submit_button:
                lines.append(f"  提交按钮: {form.submit_button}")
        return "\n".join(lines)

    def _describe_navigation(self, page: PageKnowledge) -> str:
        """将导航链接描述为文本"""
        lines = []
        for n in page.navigation[:20]:
            lines.append(f"  [{n.text}] → {n.url}")
        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM"""
        from langchain_core.messages import HumanMessage
        llm = self._get_llm()
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return response.content

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 响应中提取 JSON"""
        # 尝试提取 ```json 代码块
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试直接解析
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                return json.loads(text[json_start:json_end])
            except json.JSONDecodeError:
                pass

        return None
