"""
内置技能：日期格式化工具
支持多种日期格式转换、交货日期计算、工作日计算
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any

from webagent.skills.base_skill import BaseSkill, SkillResult


class DateFormatterSkill(BaseSkill):
    """日期格式化技能插件"""

    name = "date_formatter"
    description = "日期格式化 — 支持日期格式转换、交期计算、工作日计算"
    version = "1.0.0"

    FORMATS = {
        "iso": "%Y-%m-%d",
        "cn": "%Y年%m月%d日",
        "us": "%m/%d/%Y",
        "eu": "%d/%m/%Y",
        "compact": "%Y%m%d",
        "datetime_cn": "%Y年%m月%d日 %H:%M:%S",
        "datetime_iso": "%Y-%m-%d %H:%M:%S",
    }

    async def execute(self, params: dict[str, Any]) -> SkillResult:
        operation = params.get("operation", "format")

        try:
            if operation == "format":
                return await self._format_date(params)
            elif operation == "add_days":
                return await self._add_days(params)
            elif operation == "workdays":
                return await self._add_workdays(params)
            elif operation == "today":
                return await self._get_today(params)
            elif operation == "diff":
                return await self._date_diff(params)
            else:
                return SkillResult(success=False, message=f"未知操作: {operation}")
        except Exception as e:
            return SkillResult(success=False, message=f"日期处理错误: {e}")

    async def _format_date(self, params: dict) -> SkillResult:
        """日期格式转换"""
        date_str = params.get("date", "")
        target_format = params.get("target_format", "iso")

        # 尝试解析多种输入格式
        dt = self._parse_date(date_str)
        if not dt:
            return SkillResult(success=False, message=f"无法解析日期: {date_str}")

        fmt = self.FORMATS.get(target_format, target_format)
        result = dt.strftime(fmt)
        return SkillResult(success=True, value=result)

    async def _add_days(self, params: dict) -> SkillResult:
        """添加天数"""
        date_str = params.get("date", "")
        days = int(params.get("days", 0))
        target_format = params.get("target_format", "iso")

        dt = self._parse_date(date_str) if date_str else datetime.now()
        if not dt:
            return SkillResult(success=False, message=f"无法解析日期: {date_str}")

        result_dt = dt + timedelta(days=days)
        fmt = self.FORMATS.get(target_format, target_format)
        result = result_dt.strftime(fmt)
        return SkillResult(
            success=True,
            value=result,
            message=f"{dt.strftime('%Y-%m-%d')} + {days}天 = {result}",
        )

    async def _add_workdays(self, params: dict) -> SkillResult:
        """添加工作日"""
        date_str = params.get("date", "")
        days = int(params.get("days", 0))
        target_format = params.get("target_format", "iso")

        dt = self._parse_date(date_str) if date_str else datetime.now()
        if not dt:
            return SkillResult(success=False, message=f"无法解析日期: {date_str}")

        added = 0
        current = dt
        direction = 1 if days >= 0 else -1
        remaining = abs(days)

        while added < remaining:
            current += timedelta(days=direction)
            if current.weekday() < 5:  # 周一到周五
                added += 1

        fmt = self.FORMATS.get(target_format, target_format)
        result = current.strftime(fmt)
        return SkillResult(
            success=True,
            value=result,
            message=f"{dt.strftime('%Y-%m-%d')} + {days}个工作日 = {result}",
        )

    async def _get_today(self, params: dict) -> SkillResult:
        """获取今天的日期"""
        target_format = params.get("target_format", "iso")
        fmt = self.FORMATS.get(target_format, target_format)
        result = datetime.now().strftime(fmt)
        return SkillResult(success=True, value=result)

    async def _date_diff(self, params: dict) -> SkillResult:
        """计算两个日期之间的差值"""
        date1_str = params.get("date1", "")
        date2_str = params.get("date2", "")

        dt1 = self._parse_date(date1_str)
        dt2 = self._parse_date(date2_str)

        if not dt1 or not dt2:
            return SkillResult(success=False, message="无法解析日期")

        diff = (dt2 - dt1).days
        return SkillResult(
            success=True,
            value=diff,
            message=f"{date1_str} 到 {date2_str} 相差 {diff} 天",
        )

    def _parse_date(self, date_str: str) -> datetime | None:
        """尝试解析多种日期格式"""
        if not date_str:
            return None

        formats = [
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y年%m月%d日",
            "%Y%m%d",
            "%m/%d/%Y",
            "%d/%m/%Y",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        return None
