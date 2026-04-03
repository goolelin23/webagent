"""
测试：知识库模型和存储
"""

import json
import tempfile
from pathlib import Path
import pytest

from webagent.knowledge.models import (
    ElementInfo, FormField, FormInfo, NavLink,
    PageKnowledge, SiteKnowledge,
    ExecutionStep, ExecutionPlan,
)
from webagent.knowledge.store import KnowledgeStore


class TestModels:
    """测试数据模型"""

    def test_element_info_serialization(self):
        elem = ElementInfo(
            tag="button",
            element_type="submit",
            selector="#btn-submit",
            text="提交",
            id="btn-submit",
        )
        d = elem.to_dict()
        assert d["tag"] == "button"
        assert d["selector"] == "#btn-submit"

        restored = ElementInfo.from_dict(d)
        assert restored.tag == "button"
        assert restored.text == "提交"

    def test_page_knowledge_serialization(self):
        page = PageKnowledge(
            url="https://example.com/orders",
            title="订单列表",
            page_type="list",
            elements=[
                ElementInfo(tag="button", text="新建订单", selector="#btn-new"),
            ],
            forms=[
                FormInfo(
                    form_id="search-form",
                    fields=[
                        FormField(name="keyword", field_type="text", label="搜索"),
                    ],
                ),
            ],
            navigation=[
                NavLink(text="首页", url="/dashboard"),
            ],
            business_rules=["订单创建后不可直接删除"],
        )

        d = page.to_dict()
        restored = PageKnowledge.from_dict(d)
        assert restored.url == "https://example.com/orders"
        assert len(restored.elements) == 1
        assert len(restored.forms) == 1
        assert restored.forms[0].fields[0].name == "keyword"

    def test_site_knowledge(self):
        site = SiteKnowledge(
            domain="example.com",
            base_url="https://example.com",
            site_name="测试系统",
        )

        page1 = PageKnowledge(url="https://example.com/", title="首页")
        page2 = PageKnowledge(url="https://example.com/orders", title="订单")

        site.add_page(page1)
        site.add_page(page2)

        assert len(site.pages) == 2
        assert len(site.sitemap) == 2

        # JSON 序列化/反序列化
        json_str = site.to_json()
        restored = SiteKnowledge.from_json(json_str)
        assert restored.domain == "example.com"
        assert len(restored.pages) == 2

    def test_execution_plan(self):
        plan = ExecutionPlan(
            task="创建采购订单",
            steps=[
                ExecutionStep(step_id=1, action="navigate", target="/orders/new", description="打开新建订单页"),
                ExecutionStep(step_id=2, action="fill", target="#supplier", value="供应商A", description="填写供应商"),
                ExecutionStep(step_id=3, action="click", target="#btn-submit", description="提交订单"),
            ],
            preconditions=["已登录系统"],
            expected_outcome="订单创建成功",
        )

        json_str = plan.to_json()
        data = json.loads(json_str)
        assert len(data["steps"]) == 3

        restored = ExecutionPlan.from_dict(data)
        assert restored.task == "创建采购订单"
        assert restored.steps[1].value == "供应商A"


class TestKnowledgeStore:
    """测试知识库存储"""

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = KnowledgeStore(base_dir=Path(tmpdir))

            site = SiteKnowledge(
                domain="test.com",
                base_url="https://test.com",
                site_name="测试站点",
            )
            site.add_page(PageKnowledge(
                url="https://test.com/",
                title="首页",
                page_type="dashboard",
            ))

            store.save(site)

            loaded = store.load("test.com")
            assert loaded is not None
            assert loaded.domain == "test.com"
            assert len(loaded.pages) == 1

    def test_incremental_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = KnowledgeStore(base_dir=Path(tmpdir))

            # 首次保存
            page1 = PageKnowledge(url="https://test.com/", title="首页")
            store.update_page("test.com", page1)

            # 增量更新
            page2 = PageKnowledge(url="https://test.com/orders", title="订单")
            store.update_page("test.com", page2)

            loaded = store.load("test.com")
            assert len(loaded.pages) == 2

    def test_list_sites(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = KnowledgeStore(base_dir=Path(tmpdir))

            site1 = SiteKnowledge(domain="a.com", base_url="https://a.com")
            site2 = SiteKnowledge(domain="b.com", base_url="https://b.com")

            store.save(site1)
            store.save(site2)

            sites = store.list_sites()
            assert len(sites) == 2
            assert "a.com" in sites
            assert "b.com" in sites

    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = KnowledgeStore(base_dir=Path(tmpdir))

            site = SiteKnowledge(domain="test.com", base_url="https://test.com")
            site.add_page(PageKnowledge(
                url="https://test.com/orders",
                title="采购订单",
                elements=[ElementInfo(tag="button", text="新建采购订单")],
            ))
            site.add_page(PageKnowledge(
                url="https://test.com/users",
                title="用户管理",
            ))
            store.save(site)

            results = store.search_pages("test.com", "采购")
            assert len(results) >= 1
