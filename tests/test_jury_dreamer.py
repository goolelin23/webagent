import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from webagent.knowledge.models import SiteKnowledge, LearnedAction
from webagent.agents.jury import JuryPanel, JuryVerdict, ReviewScore
from webagent.agents.dreamer import Dreamer


class TestJuryPanel:
    """陪审团功能测试"""

    @pytest.mark.asyncio
    async def test_review_action_approved(self):
        jury = JuryPanel()
        
        # 模拟 LLM 返回高分
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '''
        ```json
        {
            "explorer_review": {"score": 8, "reasoning": "发现新页面"},
            "business_review": {"score": 7, "reasoning": "正常业务"},
            "quality_review": {"score": 9, "reasoning": "精确"},
            "final_verdict": {
                "average_score": 8.0,
                "approved": true,
                "summary": "高分通过"
            }
        }
        ```
        '''
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        jury.llm = mock_llm

        verdict = await jury.review_action("click", "点击", {"x": 10}, "", "url", "变了")
        
        assert verdict.approved is True
        assert verdict.average_score == 8.0
        assert verdict.explorer_score == 8
        assert verdict.summary == "高分通过"

    @pytest.mark.asyncio
    async def test_review_action_rejected(self):
        jury = JuryPanel()
        
        # 模拟 LLM 返回低分
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '''
        {
            "explorer_review": {"score": 2, "reasoning": "无变化"},
            "business_review": {"score": 3, "reasoning": "没意义"},
            "quality_review": {"score": 4, "reasoning": "模糊"},
            "final_verdict": {
                "average_score": 3.0,
                "approved": false,
                "summary": "分数太低"
            }
        }
        '''
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        jury.llm = mock_llm

        verdict = await jury.review_action("hover", "悬停", {"x": 10}, "", "url", "没变")
        
        assert verdict.approved is False
        assert verdict.average_score == 3.0

    @pytest.mark.asyncio
    async def test_review_action_fallback(self):
        jury = JuryPanel()
        
        # 模拟 LLM 解析失败
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("Timeout"))
        jury.llm = mock_llm

        verdict = await jury.review_action("click", "点击", {"x": 0}, "", "", "")
        
        # 降级情况应该默认通过
        assert verdict.approved is True
        assert verdict.average_score == 6.0


class TestDreamer:
    """梦境整理功能测试"""

    def test_purge_low_quality(self):
        store = MagicMock()
        dreamer = Dreamer(store)
        
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        site.learned_actions = [
            {"action_id": "a1", "confidence": 0.9, "jury_score": 8}, # 正常保留
            {"action_id": "a2", "confidence": 0.2, "jury_score": 8}, # 置信度低，删
            {"action_id": "a3", "confidence": 0.8, "jury_score": 3}, # 评分低，删
            {"action_id": "a4", "confidence": 1.0, "jury_score": 10}, # 正常保留
        ]
        
        removed = dreamer._purge_low_quality(site)
        assert removed == 2
        assert len(site.learned_actions) == 2
        assert site.learned_actions[0]["action_id"] == "a1"
        assert site.learned_actions[1]["action_id"] == "a4"

    def test_merge_duplicates(self):
        store = MagicMock()
        dreamer = Dreamer(store)
        
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        site.learned_actions = [
            # 这三个很像
            {"action_id": "d1", "page_url_pattern": "/login", "action_type": "click", "description": "点击登录按钮", "confidence": 0.6, "success_count": 6, "total_count": 10},
            {"action_id": "d2", "page_url_pattern": "/login", "action_type": "click", "description": "点击登录", "confidence": 0.9, "success_count": 9, "total_count": 10},
            {"action_id": "d3", "page_url_pattern": "/login", "action_type": "click", "description": "点登录", "confidence": 0.4, "success_count": 4, "total_count": 10},
            # 不一样的
            {"action_id": "x1", "page_url_pattern": "/login", "action_type": "fill", "description": "填密码", "confidence": 0.8},
        ]
        
        merged = dreamer._merge_duplicates(site)
        assert merged == 2  # 合并了2次
        assert len(site.learned_actions) == 2
        
        # d2 应该保留，因为置信度最高，数量会被累加
        d2 = next(a for a in site.learned_actions if a["action_id"] == "d2")
        assert d2 is not None
        assert d2["success_count"] == 19  # 6 + 9 + 4
        assert d2["total_count"] == 30    # 10 + 10 + 10

    @pytest.mark.asyncio
    async def test_llm_summarize(self):
        store = MagicMock()
        dreamer = Dreamer(store)
        
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        site.learned_actions = [
            {"action_id": "a1", "description": "步骤1", "confidence": 1.0},
            {"action_id": "bad1", "description": "无意义", "confidence": 1.0},
            {"action_id": "dup1", "description": "等同步骤1", "confidence": 1.0},
        ]
        
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '''
        {
            "redundant_groups": [
                {
                    "action_ids": ["a1", "dup1"],
                    "keep_id": "a1"
                }
            ],
            "low_value_actions": ["bad1"],
            "knowledge_summary": "这是流程总结"
        }
        '''
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        dreamer.llm = mock_llm

        removed, summary = await dreamer._llm_summarize(site)
        assert removed == 2
        assert summary == "这是流程总结"
        assert len(site.learned_actions) == 1
        assert site.learned_actions[0]["action_id"] == "a1"

    @patch("webpilot.agents.dreamer.Path")
    def test_clean_orphaned_files(self, mock_path_cls):
        store = MagicMock()
        dreamer = Dreamer(store)
        
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        site.learned_actions = [
            {"screenshot_before": "screenshots/keep1.png", "screenshot_after": "screenshots/keep2.png"}
        ]
        
        mock_screenshots_dir = MagicMock()
        mock_screenshots_dir.exists.return_value = True
        
        f1 = MagicMock()
        f1.is_file.return_value = True
        f1.suffix = ".png"
        f1.__str__.return_value = "screenshots/keep1.png"
        
        f2 = MagicMock()
        f2.is_file.return_value = True
        f2.suffix = ".png"
        f2.__str__.return_value = "screenshots/delete_me.png"
        
        f3 = MagicMock()
        f3.is_file.return_value = True
        f3.suffix = ".txt"
        f3.__str__.return_value = "screenshots/log.txt"
        
        mock_screenshots_dir.iterdir.return_value = [f1, f2, f3]
        mock_path_cls.return_value = mock_screenshots_dir

        cleaned = dreamer._clean_orphaned_files(site)
        assert cleaned == 1
        f2.unlink.assert_called_once()
        f1.unlink.assert_not_called()
        f3.unlink.assert_not_called()
