"""
主动学习探索器
通过“状态树”进行深度交互扫描，使用大语言模型智能填表，在遇阻时记录卡点
"""

from __future__ import annotations
import asyncio
import json
import hashlib
from typing import Any

from webagent.knowledge.models import (
    PageKnowledge, SiteKnowledge, ElementInfo, FormInfo,
    StateNode, BlockedPath
)
from webagent.knowledge.store import KnowledgeStore
from webagent.prompt_engine.engine import PromptEngine
from webagent.prompt_engine.templates.explorer import DATA_MOCK_PROMPT, BLOCK_REASONING_PROMPT
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning
from webagent.utils.config import get_config, get_llm

logger = get_logger("webagent.agents.active_learner")


class ActiveLearner:
    """主动学习探索智能体"""

    def __init__(self, prompt_engine: PromptEngine, knowledge_store: KnowledgeStore):
        self.prompt_engine = prompt_engine
        self.knowledge_store = knowledge_store
        self.config = get_config()
        self.llm = get_llm()

    def _hash_dom(self, url: str, elements: list[ElementInfo], forms: list[FormInfo]) -> str:
        """根据 URL 和交互元素结构生成页面状态签名，用于聚类去重"""
        # 简单取标签和大概属性
        dom_signature = url.split("?")[0]
        for e in elements:
            dom_signature += f"{e.tag}.{e.element_type}#{e.id}"
        for f in forms:
            dom_signature += f.form_id
        return hashlib.md5(dom_signature.encode('utf-8')).hexdigest()[:12]

    async def _generate_mock_data(self, url: str, title: str, form: FormInfo) -> dict:
        """调用大模型为表单生成测试数据"""
        fields_data = []
        for f in form.fields:
            fields_data.append({
                "name": f.name or f.selector,
                "type": f.field_type,
                "label": f.label,
                "required": f.required,
                "options": f.options
            })

        prompt = DATA_MOCK_PROMPT.format(
            title=title,
            url=url,
            form_context=form.title or form.form_id,
            fields_json=json.dumps(fields_data, ensure_ascii=False, indent=2)
        )

        try:
            from langchain_core.messages import HumanMessage
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            
            content = response.content
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(content[json_start:json_end])
        except Exception as e:
            logger.warning(f"生成假数据失败: {e}")
        
        # 降级：规则引擎硬编码填充
        mock_data = {}
        for f in form.fields:
            if not f.required:
                continue
            if "email" in f.name.lower():
                mock_data[f.name] = "test@example.com"
            elif "phone" in f.name.lower():
                mock_data[f.name] = "13800138000"
            elif "password" in f.name.lower():
                mock_data[f.name] = "Pass1234!"
            elif f.field_type == "number":
                mock_data[f.name] = "1"
            elif f.options:
                mock_data[f.name] = f.options[0]
            else:
                mock_data[f.name] = "test_data"
        return mock_data

    async def _analyze_block(self, url: str, dom_snippet: str, last_action: str) -> dict:
        """分析阻塞原因"""
        prompt = BLOCK_REASONING_PROMPT.format(
            url=url,
            last_action=last_action,
            dom_snippet=dom_snippet[:1000]
        )
        try:
            from langchain_core.messages import HumanMessage
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(content[json_start:json_end])
        except Exception as e:
            logger.warning(f"分析阻塞原因失败: {e}")
            
        return {
            "reason_category": "UNKNOWN_ERROR",
            "description": "未知阻断原因",
            "suggestion_for_human": "请人工检查页面状态"
        }

    async def scan_deep(self, target_url: str, max_depth: int = 3, max_nodes: int = 50) -> SiteKnowledge:
        """
        深度交互扫描主入口
        """
        from playwright.async_api import async_playwright
        from webagent.agents.explorer import ExplorerAgent
        from urllib.parse import urlparse
        import logging

        parsed = urlparse(target_url)
        domain = parsed.netloc
        site = self.knowledge_store.load(domain) or SiteKnowledge(domain=domain, base_url=f"{parsed.scheme}://{parsed.netloc}")

        # 使用 ExplorerAgent 的页面提取能力
        extractor = ExplorerAgent(self.prompt_engine, self.knowledge_store)

        print_agent("active_learner", f"🚀 启动主动学习扫描 (Deep Interactive Scan) -> {target_url}")
        print_agent("active_learner", f"深度限制: {max_depth}, 节点限制: {max_nodes}")

        if not hasattr(site, 'state_tree'):
            site.state_tree = []
        if not hasattr(site, 'blocked_paths'):
            site.blocked_paths = []

        visited_states = {n["state_id"] for n in site.state_tree}
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.browser.headless)
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            # 初始化根节点
            await page.goto(target_url, wait_until="networkidle")
            page_know = await extractor._extract_page_knowledge(page, page.url)
            site.add_page(page_know)

            state_id = self._hash_dom(page.url, page_know.elements, page_know.forms)
            root_node = StateNode(
                state_id=state_id,
                url=page.url,
                title=page_know.title,
                dom_snapshot=f"{len(page_know.elements)} elements, {len(page_know.forms)} forms",
                interactables=[e.selector for e in page_know.elements if e.selector and e.is_visible]
            )

            state_queue = [(root_node, 0)]  # (node, depth)
            if state_id not in visited_states:
                site.state_tree.append(root_node.to_dict())
                visited_states.add(state_id)

            nodes_explored = 0

            # BFS/DFS 混合探索
            while state_queue and nodes_explored < max_nodes:
                current_node, depth = state_queue.pop(0)
                
                if depth >= max_depth:
                    continue
                    
                nodes_explored += 1
                print_agent("active_learner", f"🔍 探索节点 [{nodes_explored}]: {current_node.url} (层级 {depth})")

                # 如果不是初始状态，需要还原状态（此处为了防迷路，简单粗暴：直接刷新重进，实际可以依靠 state_node.action_to_reach 寻路，这里从简用 goto 近似处理本页）
                if page.url != current_node.url:
                    await page.goto(current_node.url, wait_until="domcontentloaded")
                    await asyncio.sleep(1)

                # TODO: 聚类截断（相同的 interactable 按钮只点一个）
                targets_to_try = current_node.interactables[:5] # 防止爆炸，单页面最多点5个元素

                for target_sel in targets_to_try:
                    print_agent("active_learner", f"  👉 试探性点击: {target_sel}")
                    try:
                        # 尝试点击
                        el = await page.query_selector(target_sel)
                        if el and await el.is_visible():
                            await el.click(timeout=3000)
                            await asyncio.sleep(1)
                            
                            new_url = page.url
                            new_page_know = await extractor._extract_page_knowledge(page, new_url)
                            site.add_page(new_page_know)
                            
                            new_state_id = self._hash_dom(new_url, new_page_know.elements, new_page_know.forms)
                            
                            if new_state_id not in visited_states:
                                # 发现了新状态
                                child_node = StateNode(
                                    state_id=new_state_id,
                                    url=new_url,
                                    title=new_page_know.title,
                                    parent_id=current_node.state_id,
                                    action_to_reach={"action": "click", "target": target_sel},
                                    interactables=[e.selector for e in new_page_know.elements if e.selector and e.is_visible]
                                )
                                site.state_tree.append(child_node.to_dict())
                                visited_states.add(new_state_id)
                                state_queue.append((child_node, depth + 1))
                                
                                # ====== 特殊逻辑：表单识别与 AI 试探填表 ======
                                if new_page_know.forms:
                                    print_agent("active_learner", f"  📝 发现表单，尝试造数测试...")
                                    for form in new_page_know.forms:
                                        mock_data = await self._generate_mock_data(new_url, new_page_know.title, form)
                                        print_agent("active_learner", f"    造数完成: {mock_data}")
                                        
                                        # 填入数据
                                        for k, v in mock_data.items():
                                            inp = await page.query_selector(f"[name='{k}']")
                                            if inp:
                                                tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                                                if tag in ("input", "textarea"):
                                                    await inp.fill(v)
                                                elif tag == "select":
                                                    await inp.select_option(label=v)
                                                    
                                        # 提交
                                        if form.submit_button:
                                            btn = await page.query_selector(form.submit_button)
                                            if btn:
                                                print_agent("active_learner", f"    提交表单...")
                                                await btn.click(timeout=5000)
                                                await asyncio.sleep(2)
                                                
                                                # 检查结果
                                                post_url = page.url
                                                post_know = await extractor._extract_page_knowledge(page, post_url)
                                                site.add_page(post_know)
                                                post_state_id = self._hash_dom(post_url, post_know.elements, post_know.forms)
                                                
                                                if post_state_id == new_state_id:
                                                    # 没跳转说明报错或被阻拦了！
                                                    print_warning("  🚧 遇到阻碍，表单未成功提交。")
                                                    analysis = await self._analyze_block(
                                                        new_url, 
                                                        ", ".join([e.text for e in new_page_know.elements[:5]]), 
                                                        "submit form"
                                                    )
                                                    print_agent("active_learner", f"    诊断卡点: {analysis['reason_category']} - {analysis['description']}")
                                                    
                                                    bp = BlockedPath(
                                                        url=new_url,
                                                        state_id=new_state_id,
                                                        action_attempted="submit_form",
                                                        target_selector=form.submit_button,
                                                        reason=analysis['reason_category']
                                                    )
                                                    site.blocked_paths.append(bp.to_dict())
                                                else:
                                                    # 填表成功，加入树
                                                    if post_state_id not in visited_states:
                                                        post_node = StateNode(
                                                            state_id=post_state_id,
                                                            url=post_url,
                                                            parent_id=new_state_id,
                                                            action_to_reach={"action": "submit", "target": form.submit_button, "data": mock_data},
                                                            interactables=[e.selector for e in post_know.elements if e.selector and e.is_visible]
                                                        )
                                                        site.state_tree.append(post_node.to_dict())
                                                        visited_states.add(post_state_id)
                                                        state_queue.append((post_node, depth + 2))
                                
                            # 回退，继续遍历其他按钮
                            await page.goto(current_node.url)
                            await asyncio.sleep(0.5)

                    except Exception as e:
                        logger.debug(f"尝试点击 {target_sel} 失败: {e}")
                        # 恢复状态
                        await page.goto(current_node.url)
                        await asyncio.sleep(0.5)

            await browser.close()

        self.knowledge_store.save(site)
        print_success(f"主动扫描完成！总共发现 {len(site.state_tree)} 个状态节点。")
        if site.blocked_paths:
            print_warning(f"⚠️ 记录了 {len(site.blocked_paths)} 个受阻路径，可使用 /resolve 让用户协助清理死角。")

        return site
