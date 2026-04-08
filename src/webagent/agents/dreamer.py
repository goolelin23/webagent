"""
梦境模式 (Dream Mode)
知识库的"睡眠整理"引擎 — 清除垃圾、合并重复、LLM总结、文件清理
模拟人脑睡眠时的记忆整理与遗忘机制
"""

from __future__ import annotations
import json
import os
import re
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher
from dataclasses import dataclass

from webagent.knowledge.models import SiteKnowledge, LearnedAction
from webagent.knowledge.store import KnowledgeStore
from webagent.prompt_engine.templates.jury import DREAM_SUMMARIZE_PROMPT
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning
from webagent.utils.llm import get_llm

logger = get_logger("webagent.agents.dreamer")


@dataclass
class DreamReport:
    """梦境整理报告"""
    domain: str
    before_count: int
    after_count: int
    removed_low_quality: int
    merged_duplicates: int
    removed_by_llm: int
    cleaned_files: int
    knowledge_summary: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "removed_low_quality": self.removed_low_quality,
            "merged_duplicates": self.merged_duplicates,
            "removed_by_llm": self.removed_by_llm,
            "cleaned_files": self.cleaned_files,
            "knowledge_summary": self.knowledge_summary,
            "timestamp": self.timestamp,
        }


class Dreamer:
    """
    梦境引擎 — 知识库自清理

    四个阶段：
      Phase 1: 🗑️ 清除垃圾 — 删除低置信度/低评分的操作
      Phase 2: 🔄 合并重复 — 合并描述相似的操作
      Phase 3: 📝 LLM 知识摘要 — LLM 分析业务流程，标记冗余
      Phase 4: 🧹 文件清理 — 删除不再被引用的截图
    """

    # 清理阈值
    MIN_CONFIDENCE = 0.3       # 低于此置信度的操作将被删除
    MIN_JURY_SCORE = 4.0       # 低于此评分的操作将被删除
    SIMILARITY_THRESHOLD = 0.8 # 描述相似度超过此值视为重复

    def __init__(self, knowledge_store: KnowledgeStore):
        self.knowledge_store = knowledge_store
        self.llm = None

    def _get_llm(self):
        if self.llm is None:
            self.llm = get_llm()
        return self.llm

    def _extract_json(self, text: str) -> dict | None:
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

    def _text_similarity(self, a: str, b: str) -> float:
        """计算两个文本的相似度"""
        return SequenceMatcher(None, a, b).ratio()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 1: 清除垃圾
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _purge_low_quality(self, site: SiteKnowledge) -> int:
        """删除低置信度 / 低评分的操作"""
        before = len(site.learned_actions)
        site.learned_actions = [
            a for a in site.learned_actions
            if a.get("confidence", 0) >= self.MIN_CONFIDENCE
            and a.get("jury_score", 10) >= self.MIN_JURY_SCORE  # 未评审的默认保留
        ]
        removed = before - len(site.learned_actions)
        if removed > 0:
            print_agent("dream", f"  🗑️ Phase 1: 清除 {removed} 条低质量操作")
        return removed

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 2: 合并重复
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _merge_duplicates(self, site: SiteKnowledge) -> int:
        """合并描述相似的操作，保留置信度最高的"""
        actions = site.learned_actions
        if len(actions) <= 1:
            return 0

        merged_count = 0
        to_remove = set()

        for i in range(len(actions)):
            if i in to_remove:
                continue
            for j in range(i + 1, len(actions)):
                if j in to_remove:
                    continue

                a, b = actions[i], actions[j]

                # 同一页面 + 同一操作类型 + 描述高度相似
                if (a.get("page_url_pattern") == b.get("page_url_pattern")
                        and a.get("action_type") == b.get("action_type")):

                    desc_sim = self._text_similarity(
                        a.get("description", ""),
                        b.get("description", ""),
                    )
                    if desc_sim >= self.SIMILARITY_THRESHOLD:
                        # 保留置信度更高的
                        if a.get("confidence", 0) >= b.get("confidence", 0):
                            # 合并计数
                            a["success_count"] = a.get("success_count", 0) + b.get("success_count", 0)
                            a["total_count"] = a.get("total_count", 0) + b.get("total_count", 0)
                            to_remove.add(j)
                        else:
                            b["success_count"] = b.get("success_count", 0) + a.get("success_count", 0)
                            b["total_count"] = b.get("total_count", 0) + a.get("total_count", 0)
                            to_remove.add(i)
                            merged_count += 1
                            break  # i 已被移除，跳出内层循环

                        merged_count += 1

        if to_remove:
            site.learned_actions = [a for idx, a in enumerate(actions) if idx not in to_remove]
            print_agent("dream", f"  🔄 Phase 2: 合并 {merged_count} 组重复操作")

        return merged_count

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 3: LLM 知识摘要
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _llm_summarize(self, site: SiteKnowledge) -> tuple[int, str]:
        """
        调用 LLM 分析已学习操作：
        - 识别业务流程
        - 标记冗余操作
        - 生成知识摘要
        """
        if not site.learned_actions:
            return 0, ""

        # 构造操作列表文本
        actions_lines = []
        for a in site.learned_actions:
            actions_lines.append(
                f"- [{a.get('action_id')}] {a.get('action_type')} @ {a.get('page_url_pattern','')}\n"
                f"  描述: {a.get('description','')}\n"
                f"  评分: {a.get('jury_score', '未评审')} | 置信度: {a.get('confidence', 0):.0%}"
            )
        actions_text = "\n".join(actions_lines)

        prompt = DREAM_SUMMARIZE_PROMPT.format(
            domain=site.domain,
            site_name=site.site_name or site.domain,
            actions_text=actions_text,
        )

        removed_count = 0
        knowledge_summary = ""

        try:
            from langchain_core.messages import HumanMessage
            llm = self._get_llm()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            data = self._extract_json(response.content)

            if data:
                # 删除 LLM 建议的低价值操作
                low_value_ids = set(data.get("low_value_actions", []))
                if low_value_ids:
                    before = len(site.learned_actions)
                    site.learned_actions = [
                        a for a in site.learned_actions
                        if a.get("action_id") not in low_value_ids
                    ]
                    removed_count = before - len(site.learned_actions)

                # 处理LLM识别的重复组
                for group in data.get("redundant_groups", []):
                    keep_id = group.get("keep_id")
                    remove_ids = set(group.get("action_ids", [])) - {keep_id}
                    if remove_ids:
                        before = len(site.learned_actions)
                        site.learned_actions = [
                            a for a in site.learned_actions
                            if a.get("action_id") not in remove_ids
                        ]
                        removed_count += before - len(site.learned_actions)

                knowledge_summary = data.get("knowledge_summary", "")

                if removed_count > 0:
                    print_agent("dream", f"  📝 Phase 3: LLM 建议删除 {removed_count} 条操作")
                if knowledge_summary:
                    print_agent("dream", f"  📝 知识摘要: {knowledge_summary}")

        except Exception as e:
            logger.warning(f"梦境 LLM 分析失败: {e}")
            print_warning(f"  📝 Phase 3: LLM 分析跳过（{e}）")

        return removed_count, knowledge_summary

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 4: 文件清理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _clean_orphaned_files(self, site: SiteKnowledge) -> int:
        """删除不再被任何已学习操作引用的截图文件"""
        # 收集所有被引用的截图路径
        referenced = set()
        for a in site.learned_actions:
            if a.get("screenshot_before"):
                referenced.add(a["screenshot_before"])
            if a.get("screenshot_after"):
                referenced.add(a["screenshot_after"])

        # 扫描截图目录
        screenshots_dir = Path("screenshots")
        if not screenshots_dir.exists():
            return 0

        cleaned = 0
        for f in screenshots_dir.iterdir():
            if f.is_file() and f.suffix == ".png":
                if str(f) not in referenced:
                    try:
                        f.unlink()
                        cleaned += 1
                    except OSError:
                        pass

        if cleaned > 0:
            print_agent("dream", f"  🧹 Phase 4: 清理 {cleaned} 张孤立截图")
        return cleaned

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 梦境主入口
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def dream(self, domain: str) -> DreamReport:
        """
        对指定站点执行梦境整理

        Args:
            domain: 站点域名
        Returns:
            DreamReport 清理报告
        """
        site = self.knowledge_store.load(domain)
        if not site:
            print_warning(f"未找到站点 {domain} 的知识库")
            return DreamReport(
                domain=domain, before_count=0, after_count=0,
                removed_low_quality=0, merged_duplicates=0,
                removed_by_llm=0, cleaned_files=0,
                knowledge_summary="站点不存在",
            )

        before_count = len(site.learned_actions)
        print_agent("dream", f"💤 开始梦境整理: {domain} ({before_count} 条已学习操作)")
        print_agent("dream", "=" * 50)

        # Phase 1
        removed_low = self._purge_low_quality(site)

        # Phase 2
        merged = self._merge_duplicates(site)

        # Phase 3
        removed_llm, summary = await self._llm_summarize(site)

        # Phase 4
        cleaned_files = self._clean_orphaned_files(site)

        after_count = len(site.learned_actions)

        # 记录梦境日志
        report = DreamReport(
            domain=domain,
            before_count=before_count,
            after_count=after_count,
            removed_low_quality=removed_low,
            merged_duplicates=merged,
            removed_by_llm=removed_llm,
            cleaned_files=cleaned_files,
            knowledge_summary=summary,
        )
        site.dream_log.append(report.to_dict())

        # 保存
        self.knowledge_store.save(site)

        print_agent("dream", "=" * 50)
        print_success(
            f"💤 梦境整理完成!\n"
            f"  操作数: {before_count} → {after_count} (减少 {before_count - after_count})\n"
            f"  清除低质量: {removed_low}\n"
            f"  合并重复: {merged}\n"
            f"  LLM建议删除: {removed_llm}\n"
            f"  清理截图: {cleaned_files}"
        )

        return report

    async def dream_all(self) -> list[DreamReport]:
        """对所有站点执行梦境整理"""
        reports = []
        sites = self.knowledge_store.list_sites()
        if not sites:
            print_warning("知识库为空，无需整理")
            return reports

        for domain in sites:
            report = await self.dream(domain)
            reports.append(report)

        return reports
