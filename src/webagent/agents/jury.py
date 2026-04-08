"""
多Agent陪审团评审引擎
三位评审员从探索价值、业务价值、技术质量三个维度交叉评分
单次 LLM 调用模拟三位评审员，控制 token 成本
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import Any

from webagent.prompt_engine.templates.jury import JURY_REVIEW_PROMPT
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning
from webagent.utils.llm import get_llm

logger = get_logger("webagent.agents.jury")


@dataclass
class ReviewScore:
    """单个评审员的评分"""
    role: str           # explorer / business / quality
    score: int          # 0-10
    reasoning: str      # 评审理由


@dataclass
class JuryVerdict:
    """陪审团最终裁决"""
    explorer_score: int = 0
    explorer_reasoning: str = ""
    business_score: int = 0
    business_reasoning: str = ""
    quality_score: int = 0
    quality_reasoning: str = ""
    average_score: float = 0.0
    approved: bool = False
    summary: str = ""

    @property
    def scores(self) -> list[ReviewScore]:
        return [
            ReviewScore("explorer", self.explorer_score, self.explorer_reasoning),
            ReviewScore("business", self.business_score, self.business_reasoning),
            ReviewScore("quality", self.quality_score, self.quality_reasoning),
        ]

    def to_dict(self) -> dict:
        return {
            "explorer": {"score": self.explorer_score, "reasoning": self.explorer_reasoning},
            "business": {"score": self.business_score, "reasoning": self.business_reasoning},
            "quality": {"score": self.quality_score, "reasoning": self.quality_reasoning},
            "average_score": self.average_score,
            "approved": self.approved,
            "summary": self.summary,
        }


class JuryPanel:
    """
    多Agent陪审团

    三位评审员：
      1. 探索评审 — 操作是否带来新发现
      2. 业务评审 — 操作是否有业务意义
      3. 质量评审 — 操作的技术可靠性
    """

    PASS_THRESHOLD = 6.0  # 平均分 >= 6 才通过

    def __init__(self):
        self.llm = None

    def _get_llm(self):
        if self.llm is None:
            self.llm = get_llm()
        return self.llm

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 响应中提取 JSON"""
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                return json.loads(text[json_start:json_end])
            except json.JSONDecodeError:
                pass
        return None

    async def review_action(
        self,
        action_type: str,
        action_description: str,
        coordinates: dict,
        selector_hint: str,
        page_url: str,
        page_change: str,
        exploration_goal: str = "",
        learned_count: int = 0,
        step_number: int = 1,
    ) -> JuryVerdict:
        """
        对一个已通过视觉验证的操作进行陪审团评审

        Args:
            action_type: 操作类型 (click/fill/...)
            action_description: 操作描述
            coordinates: 坐标
            selector_hint: CSS选择器提示
            page_url: 页面URL
            page_change: 页面变化描述
            exploration_goal: 当前探索目标
            learned_count: 已学习的操作数量
            step_number: 当前步骤号
        Returns:
            JuryVerdict 评审结果
        """
        prompt = JURY_REVIEW_PROMPT.format(
            action_type=action_type,
            action_description=action_description,
            coordinates=json.dumps(coordinates),
            selector_hint=selector_hint or "（无）",
            page_url=page_url,
            page_change=page_change or "未知",
            exploration_goal=exploration_goal or "深度探索Web系统所有功能",
            learned_count=learned_count,
            step_number=step_number,
        )

        try:
            from langchain_core.messages import HumanMessage
            llm = self._get_llm()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            data = self._extract_json(response.content)

            if data:
                er = data.get("explorer_review", {})
                br = data.get("business_review", {})
                qr = data.get("quality_review", {})
                fv = data.get("final_verdict", {})

                verdict = JuryVerdict(
                    explorer_score=int(er.get("score", 5)),
                    explorer_reasoning=er.get("reasoning", ""),
                    business_score=int(br.get("score", 5)),
                    business_reasoning=br.get("reasoning", ""),
                    quality_score=int(qr.get("score", 5)),
                    quality_reasoning=qr.get("reasoning", ""),
                    average_score=float(fv.get("average_score", 5.0)),
                    approved=bool(fv.get("approved", False)),
                    summary=fv.get("summary", ""),
                )

                # 打印评审结果
                status = "✅ 通过" if verdict.approved else "❌ 否决"
                print_agent("jury", f"  ⚖️ 陪审团评审: {status} (均分 {verdict.average_score:.1f})")
                for rs in verdict.scores:
                    icon = "🟢" if rs.score >= 6 else "🟡" if rs.score >= 4 else "🔴"
                    print_agent("jury", f"    {icon} {rs.role}: {rs.score}/10 — {rs.reasoning}")

                return verdict

        except Exception as e:
            logger.warning(f"陪审团评审失败: {e}")

        # 降级：LLM 不可用时，默认通过（避免阻塞探索）
        print_warning("  ⚖️ 陪审团评审降级：LLM不可用，默认通过")
        return JuryVerdict(
            explorer_score=6,
            business_score=6,
            quality_score=6,
            average_score=6.0,
            approved=True,
            summary="评审降级：默认通过",
        )
