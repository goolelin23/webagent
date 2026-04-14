"""
主动学习探索器 (Vision-Driven)
通过截图理解 + 视觉模型推理 + 自验证 + 自愈回退，实现深度交互扫描
所有成功操作链自动沉淀到知识库 (LearnedAction)
"""

from __future__ import annotations
import asyncio
import json
import hashlib
import time
from pathlib import Path

from webagent.knowledge.models import (
    PageKnowledge, SiteKnowledge, ElementInfo, FormInfo,
    StateNode, BlockedPath, LearnedAction,
)
from webagent.knowledge.store import KnowledgeStore
from webagent.agents.vision_engine import VisionEngine, VisionAction, VerifyResult
from webagent.agents.jury import JuryPanel
from webagent.prompt_engine.engine import PromptEngine
from webagent.prompt_engine.templates.explorer import DATA_MOCK_PROMPT, BLOCK_REASONING_PROMPT
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning, print_error
from webagent.utils.llm import get_config, get_llm

logger = get_logger("webpilot.agents.active_learner")


class ActiveLearner:
    """主动学习探索智能体 — 视觉驱动版"""

    def __init__(self, prompt_engine: PromptEngine, knowledge_store: KnowledgeStore):
        self.prompt_engine = prompt_engine
        self.knowledge_store = knowledge_store
        self.config = get_config()
        self.llm = get_llm()
        self.vision = VisionEngine()
        self.jury = JuryPanel()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # DOM 工具方法（保留兼容）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _hash_dom(self, url: str, elements: list[ElementInfo], forms: list[FormInfo]) -> str:
        """根据 URL 和交互元素结构生成页面状态签名，用于聚类去重"""
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 快照与回退
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _save_snapshot(self, page) -> dict:
        """保存页面快照用于回退"""
        return {
            "url": page.url,
            "scroll_x": await page.evaluate("window.scrollX"),
            "scroll_y": await page.evaluate("window.scrollY"),
            "timestamp": time.time(),
        }

    async def _restore_snapshot(self, page, snapshot: dict):
        """恢复到快照状态"""
        current_url = page.url
        target_url = snapshot["url"]

        if current_url != target_url:
            print_agent("active_learner", f"  ⏪ 回退页面: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded")
            await VisionEngine._wait_stable(page)

        # 恢复滚动位置
        await page.evaluate(
            f"window.scrollTo({snapshot['scroll_x']}, {snapshot['scroll_y']})"
        )
        await asyncio.sleep(0.2)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 知识沉淀
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _compute_cosine_similarity(self, v1, v2):
        import math
        if not v1 or not v2 or len(v1) != len(v2): return 0.0
        dot = sum(a*b for a, b in zip(v1, v2))
        norm_a = math.sqrt(sum(a*a for a in v1))
        norm_b = math.sqrt(sum(b*b for b in v2))
        if norm_a == 0 or norm_b == 0: return 0.0
        return dot / (norm_a * norm_b)

    def _learn_action(
        self,
        site: SiteKnowledge,
        action: VisionAction,
        screenshot_before: str,
        screenshot_after: str,
        page_url: str,
        jury_score: float = 0.0,
        jury_reasoning: str = "",
    ):
        """将成功操作沉淀到知识库"""
        from webagent.utils.llm import get_embeddings
        embed_model = get_embeddings()
        try:
            current_embedding = embed_model.embed_query(action.target_description) if embed_model else []
        except Exception:
            current_embedding = []

        action_id = f"{action.action_type}_{hashlib.md5(action.target_description.encode()).hexdigest()[:8]}"

        # 检查是否已有相同操作 (Semantic / Fuzzy Matching)
        for existing in site.learned_actions:
            if existing.get("page_url_pattern") == page_url.split("?")[0] and existing.get("action_type") == action.action_type:
                match_found = False
                existing_emb = existing.get("semantic_embedding", [])
                if existing_emb and current_embedding:
                    sim = self._compute_cosine_similarity(existing_emb, current_embedding)
                    if sim > 0.85: match_found = True
                else:
                    import difflib
                    sim = difflib.SequenceMatcher(None, existing.get("description", ""), action.target_description).ratio()
                    if sim > 0.8: match_found = True

                if match_found:
                    # 更新置信度
                    existing["success_count"] = existing.get("success_count", 0) + 1
                    existing["total_count"] = existing.get("total_count", 0) + 1
                    existing["confidence"] = existing["success_count"] / existing["total_count"]
                    if getattr(action, 'element_id', None):
                        existing["element_id"] = action.element_id
                    return

        learned = LearnedAction(
            action_id=action_id,
            page_url_pattern=page_url.split("?")[0],
            action_type=action.action_type,
            description=action.target_description,
            coordinates=action.coordinates,
            value=action.value,
            selector_hint="",
            screenshot_before=screenshot_before,
            screenshot_after=screenshot_after,
            jury_score=jury_score,
            jury_reasoning=jury_reasoning,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        if current_embedding:
            learned.semantic_embedding = current_embedding
            
        if getattr(action, 'element_id', None):
            learned.element_id = action.element_id
        site.learned_actions.append(learned.to_dict())
        print_agent("active_learner", f"  💾 学习沉淀: {action.action_type} → {action.target_description} (陈审团 {jury_score:.1f}分)")

    def _mark_action_failed(self, site: SiteKnowledge, action: VisionAction, page_url: str = ""):
        """标记一次失败操作"""
        from webagent.utils.llm import get_embeddings
        embed_model = get_embeddings()
        try:
            current_embedding = embed_model.embed_query(action.target_description) if embed_model else []
        except Exception:
            current_embedding = []

        for existing in site.learned_actions:
             if existing.get("page_url_pattern") == page_url.split("?")[0] and existing.get("action_type") == action.action_type:
                 match_found = False
                 existing_emb = existing.get("semantic_embedding", [])
                 if existing_emb and current_embedding:
                     sim = self._compute_cosine_similarity(existing_emb, current_embedding)
                     if sim > 0.85: match_found = True
                 else:
                     import difflib
                     sim = difflib.SequenceMatcher(None, existing.get("description", ""), action.target_description).ratio()
                     if sim > 0.8: match_found = True

                 if match_found:
                     existing["total_count"] = existing.get("total_count", 0) + 1
                     existing["confidence"] = existing.get("success_count", 0) / existing["total_count"]
                     return

    def _get_learned_actions_for_url(self, site: SiteKnowledge, url: str) -> list[dict]:
        """获取某个 URL 的已学习操作（支持模糊路径前缀匹配 + 置信度时间衰减）"""
        from urllib.parse import urlparse
        url_pattern = url.split("?")[0]
        parsed = urlparse(url_pattern)
        url_path = parsed.path.rstrip("/")

        now = time.time()
        results = []
        for a in site.learned_actions:
            pattern = a.get("page_url_pattern", "")
            confidence = a.get("confidence", 0)

            # 精确匹配或路径前缀匹配
            pattern_parsed = urlparse(pattern)
            pattern_path = pattern_parsed.path.rstrip("/")
            exact_match = pattern == url_pattern
            prefix_match = (
                parsed.netloc == pattern_parsed.netloc
                and url_path.startswith(pattern_path)
                and len(pattern_path) > 1  # 避免根路径 / 匹配所有
            )

            if not (exact_match or prefix_match):
                continue

            # 置信度时间衰减：每过 7 天衰减 10%
            ts_str = a.get("timestamp", "")
            if ts_str:
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(ts_str).timestamp()
                    days_elapsed = (now - ts) / 86400
                    decay = max(0.3, 1.0 - 0.1 * (days_elapsed / 7))
                    confidence *= decay
                except Exception:
                    pass

            if confidence >= 0.4:
                # 非精确匹配的结果置信度打折
                if not exact_match:
                    confidence *= 0.7
                a_copy = dict(a)
                a_copy["_effective_confidence"] = confidence
                results.append(a_copy)

        # 按有效置信度排序
        results.sort(key=lambda x: x.get("_effective_confidence", 0), reverse=True)
        return results

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 视觉驱动探索核心
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _vision_explore_page(
        self,
        page,
        site: SiteKnowledge,
        goal: str,
        max_actions_per_page: int = 8,
    ) -> list[VisionAction]:
        """
        视觉驱动的单页探索

        循环：截图 → 推理 → 执行 → 验证 → 学习/回退
        """
        action_history: list[str] = []
        successful_actions: list[VisionAction] = []
        consecutive_failures = 0
        max_consecutive_failures = 3

        import difflib
        for step in range(max_actions_per_page):
            # 1. 保存快照（用于回退）
            snapshot = await self._save_snapshot(page)

            # --- 真·利用学习闭环 ---
            reusable_action = None
            known_actions = self._get_learned_actions_for_url(site, page.url)
            for ka in known_actions:
                ka_desc = f"{ka['action_type']} → {ka['description']}"
                already_done = False
                for hist in action_history:
                    # Check if action was already done in this iteration
                    if difflib.SequenceMatcher(None, hist.lower(), ka_desc.lower()).ratio() > 0.8:
                        already_done = True
                        break
                if not already_done:
                    # Construct a VisionAction to bypass perceive
                    reusable_action = VisionAction(
                        action_type=ka.get("action_type", "click"),
                        target_description=ka.get("description", ""),
                        coordinates=ka.get("coordinates", {}),
                        value=ka.get("value", ""),
                        element_id=ka.get("element_id", ""),
                        selector_hint=ka.get("selector_hint", ""),
                        reasoning="从知识库复用",
                    )
                    break

            if reusable_action:
                print_agent("active_learner", f"  📚 真·命中已知操作，跳过感知直接执行: {reusable_action.target_description}")
                vision_action = reusable_action
            else:
                # 2. 截图 + 感知
                print_agent("vision", f"  👁️ 视觉感知 (步骤 {step+1}/{max_actions_per_page})...")
                vision_action = await self.vision.perceive(page, goal, action_history)

            # 3. 死胡同检测
            if vision_action.is_dead_end:
                print_agent("active_learner", f"  🚫 到达死胡同: {vision_action.dead_end_reason}")
                break

            # 3.1 沙盒危险隔离检测
            if getattr(vision_action, 'risk_level', 'safe') == 'dangerous':
                print_agent("active_learner", f"  ⛔ 危险操作拦截: {vision_action.target_description} (被判定为危险的写操作)")
                action_history.append(f"[受阻] {vision_action.target_description} — (由沙盒协议拦截)")
                break

            action_desc = f"{vision_action.action_type} → {vision_action.target_description}"
            print_agent("active_learner", f"  🎯 决策: {action_desc} (坐标: {vision_action.coordinates})")

            # 4. 操作前截图（用于验证对比）+ 页面快照指纹
            screenshot_before = await self.vision._screenshot(page, "before")
            page_snapshot_before = await self.vision.get_page_snapshot(page)

            # 5. 执行操作
            exec_success = await self.vision.execute_vision_action(page, vision_action)

            if not exec_success:
                print_warning(f"  ❌ 操作执行失败: {action_desc}")
                await self._restore_snapshot(page, snapshot)
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    print_warning(f"  🛑 连续失败 {max_consecutive_failures} 次，停止当前页面探索")
                    break
                continue

            # 6. 等待页面响应（使用智能等待代替 sleep(1)）
            from webagent.agents.vision_engine import VisionEngine
            await VisionEngine._wait_stable(page)

            # 7. 快速验证（优先，无需 LLM 调用）
            verify_result = await self.vision.quick_verify(
                page, page_snapshot_before, action_desc
            )
            screenshot_after = ""

            # 如果快速验证不确定，才回退到 LLM 验证
            if verify_result is None:
                logger.debug(f"快速验证不确定，回退到 LLM 验证: {action_desc}")
                verify_result, screenshot_after = await self.vision.verify(
                    page, screenshot_before, action_desc
                )
            else:
                # 快速验证有结果时，只在需要时截图
                if verify_result.success:
                    screenshot_after = await self.vision._screenshot(page, "after")

            if verify_result.success:
                print_success(f"  ✅ 验证通过: {verify_result.change_description}")
                action_history.append(f"[成功] {action_desc}")
                successful_actions.append(vision_action)
                consecutive_failures = 0

                # 8. 陪审团评审（优化：简单操作跳过评审）
                # 对菜单点击、滚动等低风险操作跳过 jury，节省 LLM 调用
                simple_actions = {"scroll_down", "scroll_up", "hover"}
                skip_jury = (
                    vision_action.action_type in simple_actions
                    or (vision_action.action_type == "click" and step < 3)  # 前3步的点击跳过评审
                )

                if skip_jury:
                    # 直接沉淀，给默认评分
                    self._learn_action(
                        site, vision_action,
                        screenshot_before, screenshot_after or "",
                        page.url,
                        jury_score=7.0,
                        jury_reasoning="简单操作自动通过",
                    )
                else:
                    verdict = await self.jury.review_action(
                        action_type=vision_action.action_type,
                        action_description=vision_action.target_description,
                        coordinates=vision_action.coordinates,
                        selector_hint=vision_action.selector_hint,
                        page_url=page.url,
                        page_change=verify_result.change_description,
                        exploration_goal=goal,
                        learned_count=len(site.learned_actions),
                        step_number=step + 1,
                    )

                    if verdict.approved:
                        # 9. 通过评审 → 沉淀到知识库
                        self._learn_action(
                            site, vision_action,
                            screenshot_before, screenshot_after or "",
                            page.url,
                            jury_score=verdict.average_score,
                            jury_reasoning=verdict.summary,
                        )
                    else:
                        print_warning(
                            f"  ❌ 陪审团否决 ({verdict.average_score:.1f}分): {verdict.summary}"
                        )
                        action_history.append(f"[否决] {action_desc} — {verdict.summary}")

                # 如果页面发生了导航变化，可能需要递归探索
                if verify_result.page_changed and page.url != snapshot["url"]:
                    print_agent("active_learner", f"  📄 页面切换到: {page.url}")
                    # 不在这里递归，交给外层循环处理
            else:
                # 验证失败 → 自愈回退
                print_warning(f"  ⚠️ 验证失败: {verify_result.change_description}")
                if verify_result.error_detected:
                    print_error(f"  🔴 检测到错误: {verify_result.error_message}")

                action_history.append(f"[失败] {action_desc} — {verify_result.suggestion}")
                self._mark_action_failed(site, vision_action, page.url)

                # 回退到操作前快照
                await self._restore_snapshot(page, snapshot)
                consecutive_failures += 1

                if consecutive_failures >= max_consecutive_failures:
                    print_warning(f"  🛑 连续失败 {max_consecutive_failures} 次，停止当前页面探索")
                    break

        return successful_actions

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 深度扫描主入口
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def scan_deep(self, target_url: str, max_depth: int = 3, max_nodes: int = 50) -> SiteKnowledge:
        """
        视觉驱动的深度交互扫描主入口
        """
        from playwright.async_api import async_playwright
        from webagent.agents.explorer import ExplorerAgent
        from urllib.parse import urlparse

        parsed = urlparse(target_url)
        domain = parsed.netloc
        site = self.knowledge_store.load(domain) or SiteKnowledge(
            domain=domain,
            base_url=f"{parsed.scheme}://{parsed.netloc}"
        )

        print_agent("active_learner", f"🚀 启动视觉驱动主动学习扫描 → {target_url}")
        print_agent("active_learner", f"深度限制: {max_depth}, 节点限制: {max_nodes}")
        print_agent("active_learner", "👁️ 模式: 截图理解 + 视觉操作 + 自验证 + 自愈回退")

        if not hasattr(site, 'state_tree'):
            site.state_tree = []
        if not hasattr(site, 'blocked_paths'):
            site.blocked_paths = []
        if not hasattr(site, 'learned_actions'):
            site.learned_actions = []

        visited_urls: set[str] = set()
        explore_queue: list[tuple[str, int, float]] = [(target_url, 0, 1.0)]  # (url, depth, curiosity_score)
        nodes_explored = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.browser.headless)

            # 加载凭证
            auth_path = self.knowledge_store.get_auth_path(domain)
            context_kwargs = {"viewport": {"width": 1280, "height": 800}}
            if auth_path.exists():
                context_kwargs["storage_state"] = str(auth_path)
                print_agent("active_learner", "🔑 已加载持久化登录凭证")

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            while explore_queue and nodes_explored < max_nodes:
                # 按好奇心分数排序（贪婪策略，优先探索未见过的新页面）
                explore_queue.sort(key=lambda x: x[2], reverse=True)
                current_url, depth, curiosity = explore_queue.pop(0)

                if depth >= max_depth:
                    continue

                url_key = current_url.split("?")[0]
                if url_key in visited_urls:
                    continue
                visited_urls.add(url_key)

                nodes_explored += 1
                print_agent("active_learner", f"\n{'='*50}")
                print_agent("active_learner", f"🔍 探索节点 [{nodes_explored}]: {current_url} (层级 {depth}, 好奇心 {curiosity:.2f})")
                print_agent("active_learner", f"{'='*50}")

                try:
                    await page.goto(current_url, wait_until="domcontentloaded", timeout=15000)
                    await VisionEngine._wait_stable(page)
                except Exception as e:
                    logger.warning(f"无法导航到 {current_url}: {e}")
                    continue

                # 检测登录页面
                is_login = await self._detect_login_page(page)
                if is_login:
                    auth_path = self.knowledge_store.get_auth_path(domain)
                    if auth_path.exists():
                        print_agent("active_learner", "🔑 检测到登录页，已有保存的凭证，跳过")
                    else:
                        print_warning("🔒 检测到登录页，无保存凭证，记录为受阻路径")
                        site.blocked_paths.append(BlockedPath(
                            url=current_url,
                            state_id="login_detected",
                            action_attempted="page_load",
                            target_selector="",
                            reason="login_required",
                        ).to_dict())
                    continue

                # 提取 DOM 知识（保留，用于知识库）
                try:
                    extractor = ExplorerAgent(self.prompt_engine, self.knowledge_store)
                    page_know = await extractor._extract_page_knowledge(page, page.url)
                    site.add_page(page_know)
                except Exception as e:
                    logger.debug(f"DOM 提取失败: {e}")

                # ====== 核心：视觉驱动探索本页 ======
                goal = f"深度探索Web系统的功能。当前页面URL: {page.url}。请找到并尝试所有可点击的菜单、按钮、链接、下拉框等交互元素。"

                # 先检查是否有已学习的操作可直接复用
                known_actions = self._get_learned_actions_for_url(site, page.url)
                if known_actions:
                    print_agent("active_learner", f"  📚 发现 {len(known_actions)} 条已学习操作，优先复用")

                successful_actions = await self._vision_explore_page(
                    page, site, goal, max_actions_per_page=8,
                )

                # 收集本页探索中发现的新 URL
                current_page_url = page.url
                if current_page_url != current_url:
                    url_key_new = current_page_url.split("?")[0]
                    if url_key_new not in visited_urls:
                        score = self._compute_curiosity_score(current_page_url, visited_urls, site)
                        explore_queue.append((current_page_url, depth + 1, score))

                # 从 DOM 提取到的导航链接也加入队列
                if page.url == current_url:
                    try:
                        page_know_latest = site.get_page(current_url)
                        if page_know_latest:
                            for nav in page_know_latest.navigation[:10]:
                                nav_url_key = nav.url.split("?")[0]
                                if nav.url and nav_url_key not in visited_urls:
                                    score = self._compute_curiosity_score(nav.url, visited_urls, site)
                                    explore_queue.append((nav.url, depth + 1, score))
                    except Exception:
                        pass

                # 定期保存
                if nodes_explored % 3 == 0:
                    self.knowledge_store.save(site)

            await browser.close()

        self.knowledge_store.save(site)
        print_agent("active_learner", f"\n{'='*60}")
        print_success(
            f"视觉驱动扫描完成！\n"
            f"  探索节点: {nodes_explored}\n"
            f"  状态树节点: {len(site.state_tree)}\n"
            f"  已学习操作: {len(site.learned_actions)}"
        )
        if site.blocked_paths:
            print_warning(f"⚠️ 记录了 {len(site.blocked_paths)} 个受阻路径，可使用 /resolve 让用户协助清理死角。")

        return site

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 好奇心驱动探索 & 登录检测
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _compute_curiosity_score(self, url: str, visited_urls: set[str], site: SiteKnowledge) -> float:
        """
        计算 URL 的好奇心探索分数 (0~1)

        评分策略:
          - 路径前缀越新颖 → 分数越高
          - 已有大量已学习操作的页面 → 分数降低
          - 深层路径 (path segment多) → 略微降低（防止陷入无限深层）
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        segments = [s for s in path.split("/") if s]

        score = 1.0

        # 1. 路径前缀新颖度 — 检查已访问的 URL 中有多少共享相同前缀
        prefix_overlap_count = 0
        for visited in visited_urls:
            v_parsed = urlparse(visited)
            v_path = v_parsed.path.rstrip("/")
            # 共享至少 2 级路径前缀视为相似
            if v_parsed.netloc == parsed.netloc:
                v_segments = [s for s in v_path.split("/") if s]
                common = 0
                for a, b in zip(segments, v_segments):
                    if a == b:
                        common += 1
                    else:
                        break
                if common >= 2:
                    prefix_overlap_count += 1

        # 相似页面越多，好奇心越低
        if prefix_overlap_count > 0:
            score -= min(0.4, prefix_overlap_count * 0.1)

        # 2. 已学习操作密度 — 该页面已有操作越多，好奇心越低
        url_pattern = url.split("?")[0]
        known_count = sum(
            1 for a in site.learned_actions
            if a.get("page_url_pattern", "") == url_pattern
        )
        if known_count > 0:
            score -= min(0.3, known_count * 0.05)

        # 3. 深度惩罚 — 路径段数越多，略微降低
        if len(segments) > 4:
            score -= min(0.2, (len(segments) - 4) * 0.05)

        return max(0.05, score)  # 最低保底 0.05

    async def _detect_login_page(self, page) -> bool:
        """
        检测当前页面是否为登录页面

        启发式策略:
          - URL 包含 login/signin/auth
          - 页面存在 password 类型输入框
          - 页面标题包含登录相关关键词
        """
        url_lower = page.url.lower()
        login_url_patterns = ["/login", "/signin", "/sign-in", "/auth", "/sso", "/cas/"]
        if any(p in url_lower for p in login_url_patterns):
            return True

        try:
            title = (await page.title()).lower()
            login_title_keywords = ["登录", "login", "sign in", "signin", "log in"]
            if any(kw in title for kw in login_title_keywords):
                return True
        except Exception:
            pass

        try:
            password_count = await page.locator("input[type='password']").count()
            if password_count > 0:
                return True
        except Exception:
            pass

        return False
