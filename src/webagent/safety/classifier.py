"""
安全分类器
对所有自动生成的指令进行风险评估
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from webagent.knowledge.models import ExecutionStep
from webagent.utils.logger import get_logger, print_warning, console

logger = get_logger("webagent.safety")


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class RiskAssessment:
    """风险评估结果"""
    level: RiskLevel
    score: float = 0.0              # 0-100 风险分数
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False
    requires_approval: bool = False
    recommendation: str = ""


class SafetyClassifier:
    """安全分类器"""

    # 高风险关键词
    HIGH_RISK_KEYWORDS = [
        "删除", "delete", "remove", "drop",
        "清空", "clear", "purge", "truncate",
        "修改配置", "modify config", "change setting",
        "重置", "reset", "restore",
        "批量", "batch", "bulk", "mass",
    ]

    # 关键操作URL模式
    CRITICAL_URL_PATTERNS = [
        r"/admin/",
        r"/system/",
        r"/config/",
        r"/settings/",
        r"/permission/",
        r"/role/",
        r"/user/delete",
        r"/database/",
    ]

    # 安全操作（低风险）
    SAFE_ACTIONS = ["navigate", "screenshot", "assert", "wait", "scroll"]

    def __init__(self, safety_level: str = "medium"):
        """
        Args:
            safety_level: 安全级别 (low/medium/high)
                - low: 仅拦截 CRITICAL
                - medium: 拦截 HIGH + CRITICAL
                - high: 拦截 MEDIUM + HIGH + CRITICAL
        """
        self.safety_level = safety_level
        self._approval_callback = None

    def set_approval_callback(self, callback):
        """设置人工审批回调函数"""
        self._approval_callback = callback

    async def classify_action(self, step: ExecutionStep) -> dict[str, Any]:
        """
        对操作进行风险分类

        Returns:
            {
                "level": RiskLevel,
                "blocked": bool,
                "reason": str,
                "requires_approval": bool
            }
        """
        assessment = self._assess_risk(step)

        # 根据安全级别决定是否拦截
        should_block = self._should_block(assessment)

        if should_block:
            if assessment.requires_approval and self._approval_callback:
                # 请求人工审批
                approved = await self._request_approval(step, assessment)
                if approved:
                    logger.info(f"操作已获人工审批: 步骤{step.step_id}")
                    return {
                        "level": assessment.level.value,
                        "blocked": False,
                        "reason": "",
                        "requires_approval": False,
                    }

            logger.warning(
                f"安全拦截 [{assessment.level.value}]: "
                f"步骤{step.step_id} — {', '.join(assessment.reasons)}"
            )

        return {
            "level": assessment.level.value,
            "blocked": should_block,
            "reason": "; ".join(assessment.reasons) if should_block else "",
            "requires_approval": assessment.requires_approval,
        }

    def _assess_risk(self, step: ExecutionStep) -> RiskAssessment:
        """评估操作风险等级"""
        score = 0.0
        reasons = []

        # 1. 检查操作类型
        if step.action in self.SAFE_ACTIONS:
            return RiskAssessment(level=RiskLevel.LOW, score=0)

        # 2. 检查目标URL模式
        for pattern in self.CRITICAL_URL_PATTERNS:
            if re.search(pattern, step.target, re.IGNORECASE):
                score += 40
                reasons.append(f"目标URL包含敏感路径: {pattern}")
                break

        # 3. 检查高风险关键词
        check_text = f"{step.description} {step.value} {step.target}".lower()
        for keyword in self.HIGH_RISK_KEYWORDS:
            if keyword.lower() in check_text:
                score += 25
                reasons.append(f"包含高风险关键词: {keyword}")

        # 4. 检查操作类型的固有风险
        action_risk = {
            "click": 10,
            "fill": 5,
            "select": 5,
            "check": 5,
            "type": 5,
            "press": 15,    # 键盘操作可能触发提交
        }
        score += action_risk.get(step.action, 10)

        # 5. 确定风险等级
        if score >= 80:
            level = RiskLevel.CRITICAL
        elif score >= 50:
            level = RiskLevel.HIGH
        elif score >= 25:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW

        return RiskAssessment(
            level=level,
            score=score,
            reasons=reasons,
            requires_approval=level in (RiskLevel.HIGH, RiskLevel.CRITICAL),
            recommendation=self._get_recommendation(level),
        )

    def _should_block(self, assessment: RiskAssessment) -> bool:
        """根据安全级别判断是否应该拦截"""
        if self.safety_level == "low":
            return assessment.level == RiskLevel.CRITICAL
        elif self.safety_level == "medium":
            return assessment.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        elif self.safety_level == "high":
            return assessment.level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
        return False

    def _get_recommendation(self, level: RiskLevel) -> str:
        recommendations = {
            RiskLevel.LOW: "可以安全执行",
            RiskLevel.MEDIUM: "建议在测试环境验证后再执行",
            RiskLevel.HIGH: "需要人工审核确认",
            RiskLevel.CRITICAL: "高危操作，强制要求人工审批",
        }
        return recommendations.get(level, "")

    async def _request_approval(
        self,
        step: ExecutionStep,
        assessment: RiskAssessment,
    ) -> bool:
        """请求人工审批"""
        console.print(f"\n[bold yellow]⚠️  安全审批请求[/bold yellow]")
        console.print(f"  风险等级: [bold red]{assessment.level.value}[/bold red]")
        console.print(f"  操作: 步骤{step.step_id} — {step.description}")
        console.print(f"  动作: {step.action} → {step.target}")
        if step.value:
            console.print(f"  值: {step.value}")
        console.print(f"  风险原因:")
        for reason in assessment.reasons:
            console.print(f"    • {reason}")
        console.print(f"  建议: {assessment.recommendation}")
        console.print()

        if self._approval_callback:
            return await self._approval_callback(step, assessment)

        # 默认CLI交互式审批
        try:
            response = input("  是否允许执行? (y/n): ").strip().lower()
            return response in ("y", "yes", "是")
        except (EOFError, KeyboardInterrupt):
            return False
