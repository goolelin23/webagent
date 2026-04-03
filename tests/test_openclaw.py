import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from webagent.knowledge.models import DeepAnalysis, PageSkillDef, SiteKnowledge, WorkflowDef
from webagent.knowledge.store import KnowledgeStore
from webagent.openclaw import OpenClawExporter

class TestOpenClawExporter:
    @pytest.fixture
    def setup_knowledge(self):
        # Create a mock knowledge store with a site and analysis
        store = KnowledgeStore(base_dir=Path("./test_kb_openclaw"))
        site = SiteKnowledge(domain="test-openclaw.com", base_url="https://test-openclaw.com")
        
        analysis = DeepAnalysis(
            page_skills={
                "https://test-openclaw.com/login": [
                    PageSkillDef(
                        skill_id="login",
                        page_url="https://test-openclaw.com/login",
                        name="登录平台",
                        description="登录测试平台",
                        skill_type="login",
                        parameters=[{"name": "username", "type": "text", "required": True}],
                        steps=[{"action": "fill", "target": "#user", "value": "{username}"}]
                    ).to_dict()
                ]
            },
            workflows=[
                WorkflowDef(
                    workflow_id="login_flow",
                    name="登录流程",
                    trigger_keywords=["登录"],
                    skill_sequence=["login"]
                ).to_dict()
            ],
            business_entities=["用户"],
            system_description="测试系统"
        )
        site.set_deep_analysis(analysis)
        store.save(site)
        
        yield store, site
        
        # Cleanup
        import shutil
        if Path("./test_kb_openclaw").exists():
            shutil.rmtree("./test_kb_openclaw")

    def test_export_site(self, setup_knowledge):
        store, site = setup_knowledge
        
        with TemporaryDirectory() as tmpdir:
            exporter = OpenClawExporter(output_dir=Path(tmpdir))
            
            # Explicitly patch the exporter's store
            exporter.knowledge_store = store
            
            export_dir = exporter.export_site("test-openclaw.com")
            
            assert export_dir.exists()
            assert export_dir.name == "test-openclaw_com"
            
            # Check site overview
            overview_dir = export_dir / "site_overview"
            assert overview_dir.exists()
            assert (overview_dir / "SKILL.md").exists()
            
            overview_content = (overview_dir / "SKILL.md").read_text(encoding="utf-8")
            assert "name: webagent_test-openclaw_com_overview" in overview_content
            assert "测试系统" in overview_content
            
            # Check page skill
            skill_dir = export_dir / "skill_login"
            assert skill_dir.exists()
            assert (skill_dir / "SKILL.md").exists()
            assert (skill_dir / "skill_def.json").exists()
            
            skill_content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            assert "name: webagent_login" in skill_content
            assert "登录平台" in skill_content
            
            # Check workflow skill
            workflow_dir = export_dir / "workflow_login_flow"
            assert workflow_dir.exists()
            assert (workflow_dir / "SKILL.md").exists()
            assert (workflow_dir / "workflow_def.json").exists()
            
            workflow_content = (workflow_dir / "SKILL.md").read_text(encoding="utf-8")
            assert "name: webagent_workflow_login_flow" in workflow_content
            assert "登录流程" in workflow_content
            
            # Check knowledge base json
            kb_json = export_dir / "knowledge_base.json"
            assert kb_json.exists()
            
            kb_data = json.loads(kb_json.read_text(encoding="utf-8"))
            assert kb_data["domain"] == "test-openclaw.com"
