"""
内置技能：价格计算器
支持基础运算、折扣计算、税费计算、涨跌调整
"""

from __future__ import annotations
from typing import Any

from webagent.skills.base_skill import BaseSkill, SkillResult


class PriceCalculatorSkill(BaseSkill):
    """价格计算技能插件"""

    name = "price_calculator"
    description = "价格计算器 — 支持折扣、税费、涨跌调整等价格计算"
    version = "1.0.0"

    SUPPORTED_OPERATIONS = [
        "basic",            # 基础运算
        "discount",         # 折扣计算
        "tax",              # 税费计算
        "adjustment",       # 涨跌调整
        "total",            # 总价计算
        "unit_price",       # 单价计算
    ]

    def validate_params(self, params: dict[str, Any]) -> tuple[bool, str]:
        operation = params.get("operation")
        if not operation:
            return False, "缺少 operation 参数"
        if operation not in self.SUPPORTED_OPERATIONS:
            return False, f"不支持的操作: {operation}, 支持: {self.SUPPORTED_OPERATIONS}"
        return True, ""

    async def execute(self, params: dict[str, Any]) -> SkillResult:
        operation = params["operation"]

        try:
            if operation == "basic":
                return await self._basic_calc(params)
            elif operation == "discount":
                return await self._discount_calc(params)
            elif operation == "tax":
                return await self._tax_calc(params)
            elif operation == "adjustment":
                return await self._adjustment_calc(params)
            elif operation == "total":
                return await self._total_calc(params)
            elif operation == "unit_price":
                return await self._unit_price_calc(params)
            else:
                return SkillResult(success=False, message=f"未知操作: {operation}")
        except Exception as e:
            return SkillResult(success=False, message=f"计算错误: {e}")

    async def _basic_calc(self, params: dict) -> SkillResult:
        """基础运算: 支持 +, -, *, /"""
        a = float(params.get("a", 0))
        b = float(params.get("b", 0))
        op = params.get("op", "+")

        if op == "+":
            result = a + b
        elif op == "-":
            result = a - b
        elif op == "*":
            result = a * b
        elif op == "/":
            if b == 0:
                return SkillResult(success=False, message="除数不能为零")
            result = a / b
        else:
            return SkillResult(success=False, message=f"不支持的运算符: {op}")

        return SkillResult(
            success=True,
            value=round(result, 2),
            message=f"{a} {op} {b} = {round(result, 2)}",
        )

    async def _discount_calc(self, params: dict) -> SkillResult:
        """折扣计算"""
        price = float(params.get("price", 0))
        discount_rate = float(params.get("discount_rate", 0))  # 折扣率 0-100
        result = price * (1 - discount_rate / 100)
        return SkillResult(
            success=True,
            value=round(result, 2),
            message=f"原价 {price}, 折扣 {discount_rate}%, 折后价 {round(result, 2)}",
        )

    async def _tax_calc(self, params: dict) -> SkillResult:
        """税费计算"""
        price = float(params.get("price", 0))
        tax_rate = float(params.get("tax_rate", 13))  # 默认13%增值税
        tax_inclusive = params.get("tax_inclusive", False)  # 是否含税

        if tax_inclusive:
            tax_amount = price * tax_rate / (100 + tax_rate)
            price_without_tax = price - tax_amount
        else:
            tax_amount = price * tax_rate / 100
            price_without_tax = price

        total = price_without_tax + tax_amount

        return SkillResult(
            success=True,
            value=round(total, 2),
            message=f"不含税价 {round(price_without_tax, 2)}, 税额 {round(tax_amount, 2)}, 含税总价 {round(total, 2)}",
            metadata={
                "price_without_tax": round(price_without_tax, 2),
                "tax_amount": round(tax_amount, 2),
                "total": round(total, 2),
            },
        )

    async def _adjustment_calc(self, params: dict) -> SkillResult:
        """涨跌调整计算（如原材料暴涨50%）"""
        base_price = float(params.get("base_price", 0))
        adjustment_percent = float(params.get("adjustment_percent", 0))  # 正数为涨，负数为跌
        result = base_price * (1 + adjustment_percent / 100)

        direction = "上涨" if adjustment_percent > 0 else "下跌"
        return SkillResult(
            success=True,
            value=round(result, 2),
            message=f"基准价 {base_price}, {direction} {abs(adjustment_percent)}%, 调整后 {round(result, 2)}",
            metadata={
                "base_price": base_price,
                "adjustment_percent": adjustment_percent,
                "adjusted_price": round(result, 2),
            },
        )

    async def _total_calc(self, params: dict) -> SkillResult:
        """总价计算: 单价 × 数量"""
        unit_price = float(params.get("unit_price", 0))
        quantity = float(params.get("quantity", 1))
        result = unit_price * quantity
        return SkillResult(
            success=True,
            value=round(result, 2),
            message=f"单价 {unit_price} × 数量 {quantity} = {round(result, 2)}",
        )

    async def _unit_price_calc(self, params: dict) -> SkillResult:
        """单价计算: 总价 ÷ 数量"""
        total_price = float(params.get("total_price", 0))
        quantity = float(params.get("quantity", 1))
        if quantity == 0:
            return SkillResult(success=False, message="数量不能为零")
        result = total_price / quantity
        return SkillResult(
            success=True,
            value=round(result, 2),
            message=f"总价 {total_price} ÷ 数量 {quantity} = 单价 {round(result, 2)}",
        )
