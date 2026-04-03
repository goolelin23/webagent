import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from webagent.agents.active_learner import ActiveLearner
from webagent.knowledge.models import SiteKnowledge, FormInfo, FormField

@pytest.fixture
def active_learner():
    prompt_engine = MagicMock()
    knowledge_store = MagicMock()
    # 模拟知识库的存取
    knowledge_store.load.return_value = None
    learner = ActiveLearner(prompt_engine, knowledge_store)
    return learner

class TestActiveLearner:
    
    def test_hash_dom(self, active_learner):
        from webagent.knowledge.models import ElementInfo
        elements = [
            ElementInfo(tag="button", element_type="submit", id="btn1"),
            ElementInfo(tag="input", element_type="text", id="inp1")
        ]
        forms = [
            FormInfo(form_id="test_form")
        ]
        h1 = active_learner._hash_dom("http://test.com/a", elements, forms)
        h2 = active_learner._hash_dom("http://test.com/a?q=1", elements, forms)
        
        # 忽略 query string 应该一致
        assert h1 == h2
        
        h3 = active_learner._hash_dom("http://test.com/b", elements, forms)
        assert h1 != h3

    @pytest.mark.asyncio
    async def test_generate_mock_data_fallback(self, active_learner):
        # 模拟大模型调用失败，应走降级硬编码规则
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
        # 模拟大模型失败应返回 UNKNOWN_ERROR
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM Error"))
        active_learner.llm = mock_llm
        
        res = await active_learner._analyze_block("http://test.com", "<div>Error</div>", "click")
        assert res["reason_category"] == "UNKNOWN_ERROR"
