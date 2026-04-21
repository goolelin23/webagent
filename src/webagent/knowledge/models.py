"""
知识库数据模型
定义页面知识、元素信息、表单结构等数据结构
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any
import json


@dataclass
class ElementInfo:
    """页面交互元素信息"""
    tag: str                        # 元素标签 (button, input, a, select, etc.)
    element_type: str = ""          # 元素类型 (submit, text, password, etc.)
    selector: str = ""              # CSS 选择器
    xpath: str = ""                 # XPath
    text: str = ""                  # 元素文本
    placeholder: str = ""           # 占位文本
    name: str = ""                  # name 属性
    id: str = ""                    # id 属性
    aria_label: str = ""            # 无障碍标签
    is_visible: bool = True         # 是否可见
    is_enabled: bool = True         # 是否可用
    parent_form: str = ""           # 所属表单标识
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ElementInfo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class FormField:
    """表单字段"""
    name: str
    field_type: str         # text, select, checkbox, radio, date, number, etc.
    label: str = ""
    selector: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)   # select/radio 的选项
    validation_rules: list[str] = field(default_factory=list)
    default_value: str = ""


@dataclass
class FormInfo:
    """表单结构信息"""
    form_id: str
    action: str = ""
    method: str = ""
    title: str = ""
    fields: list[FormField] = field(default_factory=list)
    submit_button: str = ""     # 提交按钮选择器

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> FormInfo:
        fields = [FormField(**f) for f in data.get("fields", [])]
        d = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "fields"}
        return cls(fields=fields, **d)


@dataclass
class NavLink:
    """导航链接"""
    text: str
    url: str
    selector: str = ""
    parent_menu: str = ""       # 所属菜单路径
    is_active: bool = False


@dataclass
class StateNode:
    """状态树节点，表示主动学习扫描过程中的一个离散DOM状态"""
    state_id: str                   # 状态哈希指纹
    url: str                        # 关联 URL
    title: str = ""
    dom_snapshot: str = ""          # DOM片段或者特征
    parent_id: str = ""             # 父级状态节点
    action_to_reach: dict = field(default_factory=dict) # 记录如何达到此状态 {"action": "click", "target": "#submit"}
    interactables: list[str] = field(default_factory=list) # 可交互的未点击元素选择器
    is_dead_end: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> StateNode:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

@dataclass
class BlockedPath:
    """主动学习中的受阻路径，需要人工干预"""
    url: str
    state_id: str
    action_attempted: str
    target_selector: str
    reason: str                     # e.g., "captcha_required", "sms_verification", "unknown_form_error"
    screenshot_path: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> BlockedPath:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class LearnedAction:
    """视觉学习到的原子操作 — 每次成功操作都沉淀为一条记录"""
    action_id: str                  # "click_user_menu"
    page_url_pattern: str           # 可模糊匹配的 URL 片段
    action_type: str                # click, fill, scroll, select, hover
    description: str                # "点击左侧导航栏的用户管理"
    element_id: str = ""            # SOM 标签元素数字ID
    semantic_embedding: list[float] = field(default_factory=list) # 意图向量表示
    coordinates: dict = field(default_factory=dict)  # {"x": 120, "y": 340}
    value: str = ""                 # 填表时的值
    selector_hint: str = ""         # 可选的CSS选择器提示
    screenshot_before: str = ""     # 操作前截图路径
    screenshot_after: str = ""      # 操作后截图路径
    confidence: float = 1.0         # 成功次数/总执行次数
    success_count: int = 1
    total_count: int = 1
    jury_score: float = 0.0         # 陪审团评分 (0-10)
    jury_reasoning: str = ""        # 陪审团评价理由
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> LearnedAction:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PageKnowledge:
    """单页面知识"""
    url: str
    title: str = ""
    description: str = ""
    elements: list[ElementInfo] = field(default_factory=list)
    navigation: list[NavLink] = field(default_factory=list)
    forms: list[FormInfo] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)
    page_type: str = ""         # list, form, detail, dashboard, login, etc.
    screenshot_path: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PageKnowledge:
        elements = [ElementInfo.from_dict(e) for e in data.get("elements", [])]
        navigation = [NavLink(**n) for n in data.get("navigation", [])]
        forms = [FormInfo.from_dict(f) for f in data.get("forms", [])]
        business_rules = data.get("business_rules", [])
        d = {k: v for k, v in data.items()
             if k in cls.__dataclass_fields__
             and k not in ("elements", "navigation", "forms", "business_rules")}
        return cls(
            elements=elements,
            navigation=navigation,
            forms=forms,
            business_rules=business_rules,
            **d,
        )


@dataclass
class PageSkillDef:
    """
    页面技能定义 — 一个页面上可执行的原子操作
    由深度分析器自动生成
    """
    skill_id: str                   # "login", "create_order_form"
    page_url: str                   # 所属页面 URL
    name: str                       # "登录系统"
    description: str = ""           # "输入用户名密码并提交登录"
    skill_type: str = ""            # form_fill, navigation, search, crud_create, login, action
    parameters: list[dict] = field(default_factory=list)  # [{"name":"username","type":"text","required":True,"selector":"#user"}]
    steps: list[dict] = field(default_factory=list)        # 自动生成的执行步骤
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PageSkillDef:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def get_param_names(self) -> list[str]:
        return [p["name"] for p in self.parameters]

    def get_required_params(self) -> list[dict]:
        return [p for p in self.parameters if p.get("required")]


@dataclass
class WorkflowDef:
    """
    页面间工作流 — 多页面联动的完整业务流程
    """
    workflow_id: str                # "purchase_order_flow"
    name: str                       # "创建采购订单完整流程"
    description: str = ""
    trigger_keywords: list[str] = field(default_factory=list)  # ["采购", "下单", "采购订单"]
    skill_sequence: list[str] = field(default_factory=list)    # ["navigate_to_orders", "click_new", "fill_order_form"]
    pages_involved: list[str] = field(default_factory=list)    # 涉及的页面URL
    preconditions: list[str] = field(default_factory=list)     # ["需要已登录"]
    expected_outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowDef:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def matches_keywords(self, text: str) -> bool:
        """检查指令是否匹配此工作流的触发关键词"""
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in self.trigger_keywords)


@dataclass
class DeepAnalysis:
    """深度分析结果 — 扫描后由 LLM 分析生成"""
    page_skills: dict[str, list[dict]] = field(default_factory=dict)     # url → [PageSkillDef.to_dict()]
    workflows: list[dict] = field(default_factory=list)                  # [WorkflowDef.to_dict()]
    page_relationships: dict[str, list[str]] = field(default_factory=dict)  # url → [可到达的url]
    business_entities: list[str] = field(default_factory=list)              # ["采购订单", "供应商"]
    system_description: str = ""   # LLM 对系统的整体总结
    analyzed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DeepAnalysis:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def get_page_skills(self, url: str) -> list[PageSkillDef]:
        """获取指定页面的技能列表"""
        raw = self.page_skills.get(url, [])
        return [PageSkillDef.from_dict(s) for s in raw]

    def get_all_skills(self) -> list[PageSkillDef]:
        """获取所有页面技能"""
        skills = []
        for url, skill_list in self.page_skills.items():
            for s in skill_list:
                skills.append(PageSkillDef.from_dict(s))
        return skills

    def get_workflows(self) -> list[WorkflowDef]:
        """获取所有工作流"""
        if isinstance(self.workflows, list):
            return [WorkflowDef.from_dict(w) for w in self.workflows]
        return []

    def find_workflow(self, instruction: str) -> WorkflowDef | None:
        """基于指令文本匹配工作流"""
        for wf in self.get_workflows():
            if wf.matches_keywords(instruction):
                return wf
        return None

    def get_skills_prompt(self) -> str:
        """生成技能列表提示词 (极简精简版防爆显存)"""
        lines = []
        total_skills_added = 0
        for url, skill_list in self.page_skills.items():
            for s in skill_list:
                if total_skills_added > 20:  # 强制熔断，防止局部 LLM 上下文爆炸
                    lines.append("- ...(限于算力容量，更多技能被折叠)")
                    return "\n".join(lines)
                
                skill = PageSkillDef.from_dict(s)
                params_str = ", ".join(skill.get_param_names()) if skill.parameters else ""
                # 移除冗长的 detail description，仅保留名称和参数，大幅降低 Token 负担
                lines.append(f"- [{skill.skill_id}] ({params_str}) — {skill.name}")
                total_skills_added += 1
                
        return "\n".join(lines) if lines else "（暂无页面技能）"

    def get_workflows_prompt(self) -> str:
        """生成工作流提示词"""
        lines = []
        for wf in self.get_workflows():
            keywords = ", ".join(wf.trigger_keywords[:5])
            # 仅截取前三个技能作为摘要
            steps = " → ".join(wf.skill_sequence[:3]) + ("..." if len(wf.skill_sequence)>3 else "")
            lines.append(f"- {wf.name} [触发词: {keywords}]: {steps}")
        return "\n".join(lines) if lines else "（暂无工作流）"


@dataclass
class SiteKnowledge:
    """站点整体知识"""
    domain: str
    base_url: str
    site_name: str = ""
    pages: dict[str, PageKnowledge] = field(default_factory=dict)  # url -> PageKnowledge
    sitemap: list[str] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)  # 旧字段保留兼容
    deep_analysis: dict | None = field(default=None)               # DeepAnalysis.to_dict()
    state_tree: list[dict] = field(default_factory=list)           # StateNode.to_dict() 列表
    blocked_paths: list[dict] = field(default_factory=list)        # BlockedPath.to_dict() 列表
    learned_actions: list[dict] = field(default_factory=list)      # LearnedAction.to_dict() 列表
    dream_log: list[dict] = field(default_factory=list)            # 梦境清理历史
    scan_depth: int = 0
    last_scan: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> SiteKnowledge:
        pages = {
            url: PageKnowledge.from_dict(page_data)
            for url, page_data in data.get("pages", {}).items()
        }
        d = {k: v for k, v in data.items()
             if k in cls.__dataclass_fields__ and k not in ("pages",)}
        return cls(pages=pages, **d)

    @classmethod
    def from_json(cls, json_str: str) -> SiteKnowledge:
        return cls.from_dict(json.loads(json_str))

    def add_page(self, page: PageKnowledge):
        """添加或更新页面知识"""
        self.pages[page.url] = page
        if page.url not in self.sitemap:
            self.sitemap.append(page.url)

    def get_page(self, url: str) -> PageKnowledge | None:
        """获取指定URL的页面知识"""
        return self.pages.get(url)

    def get_all_forms(self) -> list[tuple[str, FormInfo]]:
        """获取站点所有表单"""
        forms = []
        for url, page in self.pages.items():
            for form in page.forms:
                forms.append((url, form))
        return forms

    def get_deep_analysis(self) -> DeepAnalysis | None:
        """获取深度分析结果"""
        if self.deep_analysis:
            return DeepAnalysis.from_dict(self.deep_analysis)
        return None

    def set_deep_analysis(self, analysis: DeepAnalysis):
        """设置深度分析结果"""
        self.deep_analysis = analysis.to_dict()

    @property
    def is_analyzed(self) -> bool:
        """是否已完成深度分析"""
        return self.deep_analysis is not None

    def summary(self) -> str:
        """生成知识库摘要"""
        total_elements = sum(len(p.elements) for p in self.pages.values())
        total_forms = sum(len(p.forms) for p in self.pages.values())
        total_nav = sum(len(p.navigation) for p in self.pages.values())

        analysis = self.get_deep_analysis()
        skill_count = len(analysis.get_all_skills()) if analysis else 0
        workflow_count = len(analysis.get_workflows()) if analysis else 0

        lines = [
            f"站点: {self.site_name or self.domain}",
            f"页面数: {len(self.pages)}",
            f"交互元素: {total_elements}",
            f"表单: {total_forms}",
            f"导航链接: {total_nav}",
            f"页面技能: {skill_count}",
            f"业务流程: {workflow_count}",
            f"已学习操作: {len(self.learned_actions)}",
            f"状态节点: {len(self.state_tree)}",
            f"受阻路径: {len(self.blocked_paths)}",
            f"深度分析: {'✅ 已完成' if self.is_analyzed else '❌ 未分析'}",
            f"最后扫描: {self.last_scan}",
        ]

        if analysis and analysis.business_entities:
            lines.append(f"业务实体: {', '.join(analysis.business_entities)}")
        if analysis and analysis.system_description:
            lines.append(f"系统描述: {analysis.system_description[:200]}")

        return "\n".join(lines)


@dataclass
class ExecutionStep:
    """执行计划中的单个步骤"""
    step_id: int
    action: str             # navigate, click, fill, select, wait, assert, scroll
    target: str = ""        # CSS selector, URL, or description
    value: str = ""         # 填入的值
    description: str = ""   # 步骤描述
    skill: str = ""         # 调用的技能插件
    skill_params: dict[str, Any] = field(default_factory=dict)
    timeout: int = 10000    # 超时时间(ms)
    optional: bool = False  # 是否可选步骤
    alternatives: list[dict] = field(default_factory=list)  # 备选操作
    from_skill: str = ""    # 记录该步骤是由哪个技能插件生成的（可选）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionPlan:
    """执行计划"""
    task: str                   # 原始任务描述
    plan_id: str = ""
    steps: list[ExecutionStep] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> ExecutionPlan:
        steps = [ExecutionStep(**s) for s in data.get("steps", [])]
        d = {k: v for k, v in data.items()
             if k in cls.__dataclass_fields__ and k != "steps"}
        return cls(steps=steps, **d)
