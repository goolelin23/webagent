"""
测试：工具管线（重试机制和安全分类器）
"""

import asyncio
import pytest

from webagent.pipeline.retry_manager import RetryManager, RetryPolicy, RetryResult
from webagent.safety.classifier import SafetyClassifier, RiskLevel
from webagent.knowledge.models import ExecutionStep
from webagent.skills.skill_manager import SkillManager


class TestRetryManager:
    """测试重试管理器"""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        manager = RetryManager(RetryPolicy(max_retries=3, base_delay=0.1))

        async def success_op():
            return "ok"

        result = await manager.execute_with_retry(success_op, description="test")
        assert result.success
        assert result.attempts == 1
        assert result.result == "ok"

    @pytest.mark.asyncio
    async def test_success_after_retry(self):
        manager = RetryManager(RetryPolicy(max_retries=3, base_delay=0.1))
        attempt_count = 0

        async def fail_then_succeed():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise Exception("临时错误")
            return "recovered"

        result = await manager.execute_with_retry(
            fail_then_succeed, description="retry test"
        )
        assert result.success
        assert result.attempts == 3

    @pytest.mark.asyncio
    async def test_all_retries_fail(self):
        manager = RetryManager(RetryPolicy(max_retries=2, base_delay=0.1))

        async def always_fail():
            raise Exception("永久错误")

        result = await manager.execute_with_retry(
            always_fail, description="fail test"
        )
        assert not result.success
        assert result.should_replan
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_fallback(self):
        manager = RetryManager(RetryPolicy(max_retries=1, base_delay=0.1))

        async def primary():
            raise Exception("主操作失败")

        async def fallback1():
            raise Exception("备选1失败")

        async def fallback2():
            return "备选2成功"

        result = await manager.execute_with_fallback(
            primary, [fallback1, fallback2], description="fallback test"
        )
        assert result.success
        assert result.result == "备选2成功"


class TestSafetyClassifier:
    """测试安全分类器"""

    @pytest.mark.asyncio
    async def test_safe_action(self):
        classifier = SafetyClassifier(safety_level="medium")
        step = ExecutionStep(
            step_id=1,
            action="navigate",
            target="https://example.com",
            description="打开首页",
        )
        result = await classifier.classify_action(step)
        assert not result["blocked"]

    @pytest.mark.asyncio
    async def test_high_risk_keyword(self):
        classifier = SafetyClassifier(safety_level="medium")
        step = ExecutionStep(
            step_id=1,
            action="click",
            target="#btn-delete-all",
            description="批量删除所有数据",
        )
        result = await classifier.classify_action(step)
        assert result["blocked"]

    @pytest.mark.asyncio
    async def test_critical_url(self):
        classifier = SafetyClassifier(safety_level="medium")
        step = ExecutionStep(
            step_id=1,
            action="click",
            target="/admin/system/config/reset",
            description="重置系统配置",
        )
        result = await classifier.classify_action(step)
        assert result["blocked"]

    @pytest.mark.asyncio
    async def test_low_safety_level(self):
        classifier = SafetyClassifier(safety_level="low")
        step = ExecutionStep(
            step_id=1,
            action="click",
            target="#delete-btn",
            description="删除记录",
        )
        result = await classifier.classify_action(step)
        # Low level 仅拦截 CRITICAL
        # 单个删除操作是 HIGH 级别，不会被 low 级别拦截
        # 具体是否拦截取决于评分


class TestSkillManager:
    """测试技能管理器"""

    def test_auto_load_builtin(self):
        manager = SkillManager()
        skills = manager.list_skills()
        names = [s["name"] for s in skills]
        assert "price_calculator" in names
        assert "date_formatter" in names

    @pytest.mark.asyncio
    async def test_price_calculator(self):
        manager = SkillManager()
        result = await manager.execute_skill("price_calculator", {
            "operation": "discount",
            "price": 1000,
            "discount_rate": 20,
        })
        assert result.success
        assert result.value == 800.0

    @pytest.mark.asyncio
    async def test_price_adjustment(self):
        manager = SkillManager()
        result = await manager.execute_skill("price_calculator", {
            "operation": "adjustment",
            "base_price": 100,
            "adjustment_percent": 50,
        })
        assert result.success
        assert result.value == 150.0

    @pytest.mark.asyncio
    async def test_date_formatter(self):
        manager = SkillManager()
        result = await manager.execute_skill("date_formatter", {
            "operation": "format",
            "date": "2025-01-15",
            "target_format": "cn",
        })
        assert result.success
        assert result.value == "2025年01月15日"

    @pytest.mark.asyncio
    async def test_unknown_skill(self):
        manager = SkillManager()
        result = await manager.execute_skill("nonexistent", {})
        assert not result.success
