"""
深度分析器和页面技能生成的测试
"""

import pytest
import asyncio
from webagent.knowledge.models import (
    PageSkillDef, WorkflowDef, DeepAnalysis,
    PageKnowledge, SiteKnowledge, ElementInfo, FormInfo, FormField,
)
from webagent.skills.page_skill_generator import DynamicPageSkill, PageSkillGenerator
from webagent.skills.skill_manager import SkillManager


class TestPageSkillDef:
    """PageSkillDef 模型测试"""

    def test_serialization(self):
        skill = PageSkillDef(
            skill_id="login",
            page_url="https://example.com/login",
            name="登录系统",
            description="输入用户名密码登录",
            skill_type="login",
            parameters=[
                {"name": "username", "type": "text", "required": True, "selector": "#user"},
                {"name": "password", "type": "password", "required": True, "selector": "#pass"},
            ],
            steps=[
                {"action": "fill", "target": "#user", "value": "{username}"},
                {"action": "fill", "target": "#pass", "value": "{password}"},
                {"action": "click", "target": "#submit"},
            ],
        )

        d = skill.to_dict()
        restored = PageSkillDef.from_dict(d)
        assert restored.skill_id == "login"
        assert len(restored.parameters) == 2
        assert len(restored.steps) == 3

    def test_param_helpers(self):
        skill = PageSkillDef(
            skill_id="test",
            page_url="https://example.com",
            name="test",
            parameters=[
                {"name": "a", "type": "text", "required": True},
                {"name": "b", "type": "text", "required": False},
                {"name": "c", "type": "select", "required": True},
            ],
        )

        assert skill.get_param_names() == ["a", "b", "c"]
        required = skill.get_required_params()
        assert len(required) == 2
        assert required[0]["name"] == "a"


class TestWorkflowDef:
    """WorkflowDef 模型测试"""

    def test_serialization(self):
        wf = WorkflowDef(
            workflow_id="create_order",
            name="创建采购订单",
            trigger_keywords=["采购", "下单", "订单"],
            skill_sequence=["login", "nav_orders", "fill_form"],
        )

        d = wf.to_dict()
        restored = WorkflowDef.from_dict(d)
        assert restored.workflow_id == "create_order"
        assert len(restored.trigger_keywords) == 3

    def test_keyword_matching(self):
        wf = WorkflowDef(
            workflow_id="test",
            name="test",
            trigger_keywords=["采购", "创建订单"],
        )

        assert wf.matches_keywords("我要创建一个采购订单") is True
        assert wf.matches_keywords("请帮我下单") is False
        assert wf.matches_keywords("创建订单") is True


class TestDeepAnalysis:
    """DeepAnalysis 模型测试"""

    def _make_analysis(self):
        return DeepAnalysis(
            page_skills={
                "https://example.com/login": [
                    PageSkillDef(
                        skill_id="login",
                        page_url="https://example.com/login",
                        name="登录",
                        description="登录系统",
                        parameters=[{"name": "username", "type": "text", "required": True}],
                    ).to_dict(),
                ],
                "https://example.com/orders": [
                    PageSkillDef(
                        skill_id="create_order",
                        page_url="https://example.com/orders",
                        name="创建订单",
                        parameters=[{"name": "product", "type": "text", "required": True}],
                    ).to_dict(),
                    PageSkillDef(
                        skill_id="search_orders",
                        page_url="https://example.com/orders",
                        name="搜索订单",
                        parameters=[{"name": "keyword", "type": "text", "required": True}],
                    ).to_dict(),
                ],
            },
            workflows=[
                WorkflowDef(
                    workflow_id="order_flow",
                    name="创建订单流程",
                    trigger_keywords=["创建订单", "下单", "采购"],
                    skill_sequence=["login", "create_order"],
                ).to_dict(),
            ],
            business_entities=["订单", "商品"],
            system_description="一个订单管理系统",
        )

    def test_serialization(self):
        analysis = self._make_analysis()
        d = analysis.to_dict()
        restored = DeepAnalysis.from_dict(d)
        assert len(restored.get_all_skills()) == 3
        assert len(restored.get_workflows()) == 1
        assert restored.system_description == "一个订单管理系统"

    def test_get_all_skills(self):
        analysis = self._make_analysis()
        skills = analysis.get_all_skills()
        assert len(skills) == 3
        ids = [s.skill_id for s in skills]
        assert "login" in ids
        assert "create_order" in ids
        assert "search_orders" in ids

    def test_find_workflow(self):
        analysis = self._make_analysis()
        wf = analysis.find_workflow("我想创建订单")
        assert wf is not None
        assert wf.workflow_id == "order_flow"

        wf2 = analysis.find_workflow("删除所有数据")
        assert wf2 is None

    def test_skills_prompt(self):
        analysis = self._make_analysis()
        prompt = analysis.get_skills_prompt()
        assert "login" in prompt
        assert "create_order" in prompt

    def test_workflows_prompt(self):
        analysis = self._make_analysis()
        prompt = analysis.get_workflows_prompt()
        assert "创建订单流程" in prompt
        assert "login" in prompt


class TestSiteKnowledgeDeepAnalysis:
    """SiteKnowledge 的深度分析集成测试"""

    def test_deep_analysis_integration(self):
        site = SiteKnowledge(domain="example.com", base_url="https://example.com")
        assert site.is_analyzed is False

        analysis = DeepAnalysis(
            page_skills={},
            business_entities=["商品"],
            system_description="测试系统",
        )

        site.set_deep_analysis(analysis)
        assert site.is_analyzed is True

        restored = site.get_deep_analysis()
        assert restored is not None
        assert restored.system_description == "测试系统"

    def test_summary_with_analysis(self):
        site = SiteKnowledge(domain="example.com", base_url="https://example.com")
        analysis = DeepAnalysis(
            page_skills={
                "https://example.com/login": [
                    PageSkillDef(skill_id="login", page_url="", name="登录").to_dict(),
                ],
            },
            workflows=[
                WorkflowDef(workflow_id="flow1", name="流程1").to_dict(),
            ],
            business_entities=["订单"],
        )
        site.set_deep_analysis(analysis)

        summary = site.summary()
        assert "✅ 已完成" in summary
        assert "页面技能: 1" in summary
        assert "业务流程: 1" in summary


class TestDynamicPageSkill:
    """DynamicPageSkill 执行测试"""

    @pytest.fixture
    def login_skill_def(self):
        return PageSkillDef(
            skill_id="login",
            page_url="https://example.com/login",
            name="登录",
            description="登录系统",
            skill_type="login",
            parameters=[
                {"name": "username", "type": "text", "required": True, "selector": "#user"},
                {"name": "password", "type": "password", "required": True, "selector": "#pass"},
            ],
            steps=[
                {"action": "fill", "target": "#user", "value": "{username}"},
                {"action": "fill", "target": "#pass", "value": "{password}"},
                {"action": "click", "target": "#submit"},
            ],
        )

    async def test_execute_renders_params(self, login_skill_def):
        skill = DynamicPageSkill(login_skill_def)
        result = await skill.execute({"username": "admin", "password": "123456"})

        assert result.success is True
        steps = result.metadata["steps"]
        assert steps[0]["value"] == "admin"
        assert steps[1]["value"] == "123456"
        assert steps[2]["target"] == "#submit"

    async def test_validate_missing_params(self, login_skill_def):
        skill = DynamicPageSkill(login_skill_def)
        ok, msg = skill.validate_params({"username": "admin"})
        assert ok is False
        assert "password" in msg

    async def test_validate_all_params(self, login_skill_def):
        skill = DynamicPageSkill(login_skill_def)
        ok, msg = skill.validate_params({"username": "admin", "password": "123"})
        assert ok is True


class TestRuleBasedSkillGeneration:
    """规则引擎页面技能生成测试"""

    def test_login_page_detection(self):
        from webagent.knowledge.deep_analyzer import DeepAnalyzer
        analyzer = DeepAnalyzer()

        page = PageKnowledge(
            url="https://example.com/login",
            title="登录",
            page_type="login",
            elements=[
                ElementInfo(tag="input", element_type="text", selector="#username"),
                ElementInfo(tag="input", element_type="password", selector="#password"),
                ElementInfo(tag="button", element_type="submit", selector="#submit", text="登录"),
            ],
        )

        skills = analyzer._rule_based_page_skills(page)
        assert len(skills) >= 1
        login_skill = next(s for s in skills if s.skill_type == "login")
        assert login_skill.skill_id == "login"
        assert len(login_skill.parameters) == 2

    def test_form_page_detection(self):
        from webagent.knowledge.deep_analyzer import DeepAnalyzer
        analyzer = DeepAnalyzer()

        page = PageKnowledge(
            url="https://example.com/orders/new",
            title="新建订单",
            page_type="form",
            forms=[
                FormInfo(
                    form_id="order_form",
                    title="订单表单",
                    fields=[
                        FormField(name="supplier", field_type="select", label="供应商", selector="#supplier", required=True, options=["A公司", "B公司"]),
                        FormField(name="product", field_type="text", label="商品名", selector="#product", required=True),
                        FormField(name="quantity", field_type="number", label="数量", selector="#qty"),
                    ],
                    submit_button="#btn-submit",
                ),
            ],
        )

        skills = analyzer._rule_based_page_skills(page)
        form_skill = next(s for s in skills if s.skill_type == "form_fill")
        assert form_skill is not None
        assert len(form_skill.parameters) == 3
        assert form_skill.parameters[0]["name"] == "supplier"
        assert form_skill.steps[-1]["target"] == "#btn-submit"
