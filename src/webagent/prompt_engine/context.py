"""
业务上下文管理器
维护和注入业务领域相关的上下文信息
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessContext:
    """业务上下文"""
    domain: str = ""                    # 业务领域（供应链、人力资源、电商等）
    system_name: str = ""               # 系统名称
    system_url: str = ""                # 系统URL
    language: str = "zh-CN"             # 系统语言
    rules: list[str] = field(default_factory=list)          # 业务规则
    terminology: dict[str, str] = field(default_factory=dict)  # 术语字典
    constraints: list[str] = field(default_factory=list)     # 操作约束
    session_vars: dict[str, Any] = field(default_factory=dict) # 会话变量

    def to_prompt_text(self) -> str:
        """生成用于提示词注入的文本"""
        parts = []

        if self.domain:
            parts.append(f"## 业务领域: {self.domain}")

        if self.system_name:
            parts.append(f"## 系统名称: {self.system_name}")

        if self.rules:
            parts.append("## 业务规则:")
            for rule in self.rules:
                parts.append(f"- {rule}")

        if self.terminology:
            parts.append("## 术语说明:")
            for term, explanation in self.terminology.items():
                parts.append(f"- {term}: {explanation}")

        if self.constraints:
            parts.append("## 操作约束:")
            for constraint in self.constraints:
                parts.append(f"- {constraint}")

        return "\n".join(parts) if parts else ""

    def set_var(self, key: str, value: Any):
        """设置会话变量"""
        self.session_vars[key] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        """获取会话变量"""
        return self.session_vars.get(key, default)


class ContextManager:
    """上下文管理器，管理多个业务上下文"""

    # 预定义的业务领域模板
    DOMAIN_TEMPLATES = {
        "supply_chain": BusinessContext(
            domain="供应链管理",
            rules=[
                "采购订单必须选择已审核的供应商",
                "采购金额超过10万需要额外审批",
                "入库操作必须关联采购订单",
                "库存不足时应触发采购建议",
            ],
            terminology={
                "PO": "采购订单(Purchase Order)",
                "PR": "采购申请(Purchase Requisition)",
                "GRN": "收货通知单(Goods Receipt Note)",
                "SKU": "库存单位(Stock Keeping Unit)",
            },
            constraints=[
                "不可删除已审批的订单",
                "修改价格需要权限验证",
                "批量操作每次不超过100条",
            ],
        ),
        "hr": BusinessContext(
            domain="人力资源管理",
            rules=[
                "员工入职必须完成所有必填信息",
                "薪资调整需要部门主管审批",
                "考勤数据需要当月完成审核",
            ],
            terminology={
                "HC": "人员编制(Head Count)",
                "OKR": "目标与关键成果",
                "KPI": "关键绩效指标",
            },
        ),
        "ecommerce": BusinessContext(
            domain="电子商务",
            rules=[
                "商品上架需要完整的图片和描述",
                "售价不得低于成本价",
                "库存为零时自动下架",
            ],
            terminology={
                "SPU": "标准产品单元",
                "SKU": "库存量单位",
                "GMV": "成交总额",
            },
        ),
    }

    def __init__(self):
        self._current: BusinessContext = BusinessContext()
        self._history: list[BusinessContext] = []

    @property
    def current(self) -> BusinessContext:
        return self._current

    def set_context(self, context: BusinessContext):
        """设置当前业务上下文"""
        self._history.append(self._current)
        self._current = context

    def load_domain_template(self, domain: str) -> BusinessContext:
        """加载预定义的业务领域模板"""
        template = self.DOMAIN_TEMPLATES.get(domain)
        if template:
            self.set_context(template)
            return template
        return self._current

    def update_from_scan(self, scan_result: dict):
        """从扫描结果中更新上下文"""
        if "business_rules" in scan_result:
            self._current.rules.extend(scan_result["business_rules"])
        if "system_name" in scan_result:
            self._current.system_name = scan_result["system_name"]

    def get_prompt_context(self) -> str:
        """获取用于提示词注入的上下文文本"""
        return self._current.to_prompt_text()

    def reset(self):
        """重置上下文"""
        self._current = BusinessContext()
        self._history.clear()
