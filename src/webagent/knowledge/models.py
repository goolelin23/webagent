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
class SiteKnowledge:
    """站点整体知识"""
    domain: str
    base_url: str
    site_name: str = ""
    pages: dict[str, PageKnowledge] = field(default_factory=dict)  # url -> PageKnowledge
    sitemap: list[str] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)  # 业务流程
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
             if k in cls.__dataclass_fields__ and k != "pages"}
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

    def summary(self) -> str:
        """生成知识库摘要"""
        total_elements = sum(len(p.elements) for p in self.pages.values())
        total_forms = sum(len(p.forms) for p in self.pages.values())
        total_nav = sum(len(p.navigation) for p in self.pages.values())
        return (
            f"站点: {self.site_name or self.domain}\n"
            f"页面数: {len(self.pages)}\n"
            f"交互元素: {total_elements}\n"
            f"表单: {total_forms}\n"
            f"导航链接: {total_nav}\n"
            f"业务流程: {len(self.workflows)}\n"
            f"最后扫描: {self.last_scan}"
        )


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
