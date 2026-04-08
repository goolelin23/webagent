import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import asdict

from webagent.knowledge.models import (
    LearnedAction, SiteKnowledge, FormInfo, FormField, ElementInfo
)
from webagent.agents.vision_engine import VisionEngine, VisionAction, VerifyResult


class TestLearnedAction:
    """LearnedAction 模型测试"""

    def test_serialization(self):
        action = LearnedAction(
            action_id="click_user_menu",
            page_url_pattern="http://test.com/admin",
            action_type="click",
            description="点击用户管理菜单",
            coordinates={"x": 120, "y": 340},
            screenshot_before="screenshots/before.png",
            screenshot_after="screenshots/after.png",
        )
        d = action.to_dict()
        assert d["action_id"] == "click_user_menu"
        assert d["coordinates"]["x"] == 120
        assert d["confidence"] == 1.0

    def test_deserialization(self):
        data = {
            "action_id": "fill_email",
            "page_url_pattern": "http://test.com/form",
            "action_type": "fill",
            "description": "填写邮箱",
            "coordinates": {"x": 300, "y": 200},
            "value": "test@example.com",
            "confidence": 0.8,
            "success_count": 4,
            "total_count": 5,
        }
        action = LearnedAction.from_dict(data)
        assert action.action_id == "fill_email"
        assert action.value == "test@example.com"
        assert action.confidence == 0.8

    def test_site_knowledge_learned_actions_field(self):
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        assert site.learned_actions == []

        action = LearnedAction(
            action_id="test_click",
            page_url_pattern="http://test.com",
            action_type="click",
            description="test",
        )
        site.learned_actions.append(action.to_dict())

        d = site.to_dict()
        assert len(d["learned_actions"]) == 1
        assert d["learned_actions"][0]["action_id"] == "test_click"

        # Round-trip
        site2 = SiteKnowledge.from_dict(d)
        assert len(site2.learned_actions) == 1


class TestVisionEngine:
    """VisionEngine 核心方法测试"""

    def test_extract_json(self):
        engine = VisionEngine()

        # 纯 JSON
        result = engine._extract_json('{"success": true, "x": 1}')
        assert result == {"success": True, "x": 1}

        # 带 ```json 代码块
        text = '这是分析结果:\n```json\n{"found": true}\n```\n请参考'
        result = engine._extract_json(text)
        assert result == {"found": True}

        # 无效
        result = engine._extract_json("无法解析")
        assert result is None

    def test_vision_action_dataclass(self):
        action = VisionAction(
            action_type="click",
            target_description="点击提交按钮",
            coordinates={"x": 500, "y": 300},
            reasoning="页面上有提交按钮",
        )
        assert action.action_type == "click"
        assert action.coordinates["x"] == 500
        assert not action.is_dead_end

    def test_verify_result_dataclass(self):
        result = VerifyResult(
            success=True,
            page_changed=True,
            change_description="页面跳转到了列表页",
        )
        assert result.success
        assert result.page_changed

    def test_vision_action_selector_hint(self):
        """VisionAction 可以携带 selector 提示"""
        action = VisionAction(
            action_type="click",
            target_description="提交按钮",
            coordinates={"x": 500, "y": 300},
            selector_hint="#submit-btn",
        )
        assert action.selector_hint == "#submit-btn"

    @pytest.mark.asyncio
    async def test_refine_coordinates_with_element(self):
        """L1精修：elementFromPoint 找到元素后吸附到中心"""
        engine = VisionEngine()
        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock(return_value={
            "original": {"x": 100, "y": 200},
            "refined": {"x": 120, "y": 210},
            "tag": "button",
            "id": "submit",
            "text": "提交",
            "role": "",
            "type": "submit",
            "href": "",
            "is_visible": True,
            "is_interactive": True,
            "bounding_box": {"x": 80, "y": 190, "width": 80, "height": 40},
            "selector_hint": "#submit",
        })

        rx, ry, selector, method = await engine._refine_coordinates(mock_page, 100, 200)
        assert rx == 120
        assert ry == 210
        assert selector == "#submit"
        assert method == "refine"

    @pytest.mark.asyncio
    async def test_refine_coordinates_fallback_to_scan(self):
        """L1失败时降级到L3附近扫描"""
        engine = VisionEngine()
        mock_page = MagicMock()

        # L1返回None（没找到元素）
        # L3返回附近元素列表
        mock_page.evaluate = AsyncMock(side_effect=[
            None,  # L1: elementFromPoint 未找到
            [{"center_x": 115, "center_y": 205, "tag": "a", "text": "用户管理",
              "id": "user-link", "distance": 15, "selector_hint": "#user-link"}],
        ])

        rx, ry, selector, method = await engine._refine_coordinates(
            mock_page, 100, 200, "用户管理菜单"
        )
        assert rx == 115
        assert ry == 205
        assert method in ("scan_text", "scan_nearest")

    @pytest.mark.asyncio
    async def test_refine_coordinates_all_fail(self):
        """所有精修策略都失败时返回原始坐标"""
        engine = VisionEngine()
        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("JS Error"))

        rx, ry, selector, method = await engine._refine_coordinates(mock_page, 100, 200)
        assert rx == 100
        assert ry == 200
        assert method == "raw"

    @pytest.mark.asyncio
    async def test_smart_click_selector_first(self):
        """智能点击优先使用选择器"""
        engine = VisionEngine()
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = MagicMock()
        mock_locator.first.is_visible = AsyncMock(return_value=True)
        mock_locator.first.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        result = await engine._smart_click(mock_page, 100, 200, "#my-button")
        assert result is True
        mock_locator.first.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_smart_click_fallback_to_coordinates(self):
        """选择器失败时降级到坐标点击"""
        engine = VisionEngine()
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock()

        result = await engine._smart_click(mock_page, 100, 200, "#missing")
        assert result is True
        mock_page.mouse.click.assert_called_once_with(100, 200)


class TestActiveLearner:
    """ActiveLearner 核心逻辑测试"""

    @pytest.fixture
    def active_learner(self):
        from webagent.agents.active_learner import ActiveLearner
        prompt_engine = MagicMock()
        knowledge_store = MagicMock()
        knowledge_store.load.return_value = None
        learner = ActiveLearner(prompt_engine, knowledge_store)
        return learner

    def test_hash_dom(self, active_learner):
        elements = [
            ElementInfo(tag="button", element_type="submit", id="btn1"),
            ElementInfo(tag="input", element_type="text", id="inp1")
        ]
        forms = [FormInfo(form_id="test_form")]

        h1 = active_learner._hash_dom("http://test.com/a", elements, forms)
        h2 = active_learner._hash_dom("http://test.com/a?q=1", elements, forms)
        assert h1 == h2  # 忽略 query string

        h3 = active_learner._hash_dom("http://test.com/b", elements, forms)
        assert h1 != h3

    @pytest.mark.asyncio
    async def test_generate_mock_data_fallback(self, active_learner):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM Error"))
        active_learner.llm = mock_llm

        form = FormInfo(
            form_id="user_form",
            fields=[
                FormField(name="email", field_type="text", required=True),
                FormField(name="age", field_type="number", required=True),
                FormField(name="optional_field", field_type="text", required=False),
            ]
        )

        mock_data = await active_learner._generate_mock_data("http://test.com", "Test", form)
        assert "email" in mock_data
        assert mock_data["email"] == "test@example.com"
        assert "age" in mock_data
        assert mock_data["age"] == "1"
        assert "optional_field" not in mock_data

    @pytest.mark.asyncio
    async def test_analyze_block_fallback(self, active_learner):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM Error"))
        active_learner.llm = mock_llm

        res = await active_learner._analyze_block("http://test.com", "<div>Error</div>", "click")
        assert res["reason_category"] == "UNKNOWN_ERROR"

    def test_learn_action(self, active_learner):
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        action = VisionAction(
            action_type="click",
            target_description="点击用户管理",
            coordinates={"x": 100, "y": 200},
        )

        active_learner._learn_action(site, action, "before.png", "after.png", "http://test.com/admin")
        assert len(site.learned_actions) == 1
        assert site.learned_actions[0]["action_type"] == "click"

        # 第二次学习同一操作应该增加 success_count
        active_learner._learn_action(site, action, "before2.png", "after2.png", "http://test.com/admin")
        assert len(site.learned_actions) == 1
        assert site.learned_actions[0]["success_count"] == 2

    def test_mark_action_failed(self, active_learner):
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        action = VisionAction(
            action_type="click",
            target_description="点击删除",
            coordinates={"x": 500, "y": 400},
        )

        # 先学习成功
        active_learner._learn_action(site, action, "b.png", "a.png", "http://test.com")
        assert site.learned_actions[0]["confidence"] == 1.0

        # 标记一次失败
        active_learner._mark_action_failed(site, action)
        assert site.learned_actions[0]["total_count"] == 2
        assert site.learned_actions[0]["confidence"] == 0.5

    def test_get_learned_actions_for_url(self, active_learner):
        site = SiteKnowledge(domain="test.com", base_url="http://test.com")
        site.learned_actions = [
            {"action_id": "a1", "page_url_pattern": "http://test.com/admin", "confidence": 0.9},
            {"action_id": "a2", "page_url_pattern": "http://test.com/users", "confidence": 0.8},
            {"action_id": "a3", "page_url_pattern": "http://test.com/admin", "confidence": 0.3},  # 低置信度
        ]

        results = active_learner._get_learned_actions_for_url(site, "http://test.com/admin?tab=1")
        assert len(results) == 1  # 只有 a1（a3 置信度 < 0.5）
        assert results[0]["action_id"] == "a1"

    @pytest.mark.asyncio
    async def test_save_restore_snapshot(self, active_learner):
        mock_page = MagicMock()
        mock_page.url = "http://test.com/page1"
        mock_page.evaluate = AsyncMock(side_effect=[100, 200, None])  # scrollX, scrollY, scrollTo
        mock_page.goto = AsyncMock()

        snapshot = await active_learner._save_snapshot(mock_page)
        assert snapshot["url"] == "http://test.com/page1"
        assert snapshot["scroll_x"] == 100
        assert snapshot["scroll_y"] == 200

        # 模拟页面已变化
        mock_page.url = "http://test.com/page2"
        mock_page.evaluate = AsyncMock()
        await active_learner._restore_snapshot(mock_page, snapshot)
        mock_page.goto.assert_called_once()
