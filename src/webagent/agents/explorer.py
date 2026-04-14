"""
探索Agent
使用 Browser Use 对 Web 系统进行扫描，生成知识库
"""

from __future__ import annotations
import asyncio
import json
from urllib.parse import urlparse, urljoin
from typing import Any

from webagent.knowledge.models import (
    PageKnowledge, SiteKnowledge, ElementInfo, FormInfo,
    FormField, NavLink,
)
from webagent.knowledge.store import KnowledgeStore
from webagent.prompt_engine.engine import PromptEngine
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning
from webagent.utils.config import get_config
from webagent.utils.llm import get_llm

logger = get_logger("webpilot.agents.explorer")


class ExplorerAgent:
    """
    探索Agent — 使用 Browser Use 对Web系统进行扫描
    负责在各个页面穿梭，记录交互元素，最终生成知识库
    """

    def __init__(
        self,
        prompt_engine: PromptEngine,
        knowledge_store: KnowledgeStore,
    ):
        self.prompt_engine = prompt_engine
        self.knowledge_store = knowledge_store
        self.config = get_config()
        self._visited_urls: set[str] = set()

    async def scan_site(
        self,
        target_url: str,
        scan_depth: int = 2,
        max_pages: int = 50,
        auto_analyze: bool = True,
    ) -> SiteKnowledge:
        """
        扫描整个站点

        Args:
            target_url: 目标URL
            scan_depth: 扫描深度
            max_pages: 最大页面数
            auto_analyze: 扫描后自动进行深度分析
        Returns:
            SiteKnowledge 站点知识
        """
        parsed = urlparse(target_url)
        domain = parsed.netloc
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        print_agent("explorer", f"开始扫描站点: {target_url}")
        print_agent("explorer", f"扫描深度: {scan_depth}, 最大页面: {max_pages}")

        # 加载或创建站点知识
        site = self.knowledge_store.load(domain) or SiteKnowledge(
            domain=domain,
            base_url=base_url,
        )

        try:
            # 使用 Browser Use 进行扫描
            await self._browser_use_scan(
                target_url=target_url,
                site=site,
                scan_depth=scan_depth,
                max_pages=max_pages,
            )
        except ImportError:
            logger.warning("browser-use 未安装，使用 Playwright 直接扫描")
            await self._playwright_scan(
                target_url=target_url,
                site=site,
                scan_depth=scan_depth,
                max_pages=max_pages,
            )

        # 保存知识库（扫描结果）
        self.knowledge_store.save(site)
        print_success(f"扫描完成! 共发现 {len(site.pages)} 个页面")

        # 深度分析: LLM 分析每个页面的原子操作，生成技能和工作流
        if auto_analyze and site.pages:
            await self.deep_analyze(site)

        print_agent("explorer", f"\n{site.summary()}")
        return site

    async def deep_analyze(self, site: SiteKnowledge) -> SiteKnowledge:
        """
        对已扫描的站点进行深度分析（也可手动调用）

        分析内容:
        - 每个页面的原子操作 → 自动生成 PageSkillDef
        - 跨页面的业务流程 → 自动生成 WorkflowDef
        - 系统整体描述和业务实体识别
        """
        from webagent.knowledge.deep_analyzer import DeepAnalyzer

        analyzer = DeepAnalyzer()
        analysis = await analyzer.analyze(site)

        # 保存深度分析结果到知识库
        site.set_deep_analysis(analysis)
        self.knowledge_store.save(site)

        return site

    async def _browser_use_scan(
        self,
        target_url: str,
        site: SiteKnowledge,
        scan_depth: int,
        max_pages: int,
    ):
        """使用 Browser Use 进行智能扫描"""
        from browser_use import Agent, Browser

        llm = get_llm()

        # 构建探索任务提示词
        task_prompt = self.prompt_engine.build_explorer_task(
            target_url=target_url,
            scan_depth=scan_depth,
            known_info=site.summary() if site.pages else "",
        )

        system_prompt = self.prompt_engine.get_explorer_system_prompt()

        browser = Browser()

        agent = Agent(
            task=task_prompt,
            llm=llm,
            browser=browser,
            extend_system_message=system_prompt,
        )

        print_agent("explorer", "Browser Use Agent 启动中...")

        result = await agent.run()

        # 解析 Agent 返回的结果
        if result:
            await self._parse_browser_use_result(result, site, target_url)

        print_agent("explorer", "Browser Use 扫描阶段完成")

    async def _playwright_scan(
        self,
        target_url: str,
        site: SiteKnowledge,
        scan_depth: int,
        max_pages: int,
    ):
        """使用 Playwright 直接扫描（Browser Use 不可用时的降级方案）"""
        from playwright.async_api import async_playwright

        print_agent("explorer", "使用 Playwright 直接扫描模式")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.browser.headless)
            
            auth_path = self.knowledge_store.get_auth_path(site.domain)
            context_kwargs = {
                "viewport": {
                    "width": self.config.browser.viewport_width,
                    "height": self.config.browser.viewport_height,
                }
            }
            if auth_path.exists():
                context_kwargs["storage_state"] = str(auth_path)
                print_agent("explorer", "🔑 已加载持久化登录凭证")
                
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            # BFS 扫描
            queue = [(target_url, 0)]  # (url, depth)
            visited = set()

            # ── 自动发现 sitemap.xml / robots.txt ──
            extra_urls = await self._discover_sitemap_urls(page, site.base_url)
            if extra_urls:
                print_agent("explorer", f"🗺️ 从 sitemap/robots.txt 发现 {len(extra_urls)} 个额外URL")
                for eu in extra_urls:
                    queue.append((eu, 1))

            while queue and len(visited) < max_pages:
                url, depth = queue.pop(0)

                if url in visited or depth > scan_depth:
                    continue

                if not url.startswith(site.base_url):
                    continue

                visited.add(url)
                print_agent("explorer", f"扫描页面 [{len(visited)}/{max_pages}]: {url}")

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    from webagent.agents.vision_engine import VisionEngine
                    await VisionEngine._wait_stable(page)

                    # 提取页面知识
                    page_knowledge = await self._extract_page_knowledge(page, url)
                    site.add_page(page_knowledge)

                    # 发现新链接
                    if depth < scan_depth:
                        links = await self._extract_links(page, site.base_url)
                        for link in links:
                            if link not in visited:
                                queue.append((link, depth + 1))

                except Exception as e:
                    logger.warning(f"扫描页面失败 [{url}]: {e}")
                    continue

            await browser.close()

    async def _extract_page_knowledge(self, page, url: str) -> PageKnowledge:
        """从 Playwright 页面提取知识"""
        title = await page.title()

        # 提取所有交互元素
        elements = await self._extract_elements(page)

        # 提取表单
        forms = await self._extract_forms(page)

        # 提取导航链接
        navigation = await self._extract_navigation(page)

        # 判断页面类型
        page_type = await self._detect_page_type(page, elements, forms)

        return PageKnowledge(
            url=url,
            title=title,
            elements=elements,
            forms=forms,
            navigation=navigation,
            page_type=page_type,
        )

    async def _extract_elements(self, page) -> list[ElementInfo]:
        """提取页面交互元素"""
        elements = []

        # 提取按钮
        buttons = await page.query_selector_all("button, input[type='button'], input[type='submit'], a.btn, [role='button']")
        for btn in buttons:
            try:
                info = ElementInfo(
                    tag=await btn.evaluate("el => el.tagName.toLowerCase()"),
                    element_type=await btn.get_attribute("type") or "",
                    text=(await btn.text_content() or "").strip()[:100],
                    id=await btn.get_attribute("id") or "",
                    name=await btn.get_attribute("name") or "",
                    aria_label=await btn.get_attribute("aria-label") or "",
                    is_visible=await btn.is_visible(),
                )
                # 生成选择器
                if info.id:
                    info.selector = f"#{info.id}"
                elif info.name:
                    info.selector = f"[name='{info.name}']"
                elements.append(info)
            except Exception:
                continue

        # 提取输入框
        inputs = await page.query_selector_all("input:not([type='hidden']), textarea, select")
        for inp in inputs:
            try:
                info = ElementInfo(
                    tag=await inp.evaluate("el => el.tagName.toLowerCase()"),
                    element_type=await inp.get_attribute("type") or "text",
                    placeholder=await inp.get_attribute("placeholder") or "",
                    id=await inp.get_attribute("id") or "",
                    name=await inp.get_attribute("name") or "",
                    aria_label=await inp.get_attribute("aria-label") or "",
                    is_visible=await inp.is_visible(),
                    is_enabled=await inp.is_enabled(),
                )
                if info.id:
                    info.selector = f"#{info.id}"
                elif info.name:
                    info.selector = f"[name='{info.name}']"
                elements.append(info)
            except Exception:
                continue

        return elements

    async def _extract_forms(self, page) -> list[FormInfo]:
        """提取表单结构"""
        forms = []
        form_elements = await page.query_selector_all("form")

        for i, form_el in enumerate(form_elements):
            try:
                form_id = await form_el.get_attribute("id") or f"form_{i}"
                action = await form_el.get_attribute("action") or ""
                method = await form_el.get_attribute("method") or "GET"

                # 提取表单字段
                field_elements = await form_el.query_selector_all(
                    "input:not([type='hidden']), textarea, select"
                )
                fields = []
                for field_el in field_elements:
                    try:
                        # 尝试找到关联的 label
                        field_id = await field_el.get_attribute("id") or ""
                        label_text = ""
                        if field_id:
                            label = await page.query_selector(f"label[for='{field_id}']")
                            if label:
                                label_text = (await label.text_content() or "").strip()

                        field_name = await field_el.get_attribute("name") or ""
                        field_type = await field_el.get_attribute("type") or "text"
                        tag = await field_el.evaluate("el => el.tagName.toLowerCase()")

                        if tag == "select":
                            field_type = "select"
                            options = await field_el.query_selector_all("option")
                            option_texts = []
                            for opt in options:
                                opt_text = (await opt.text_content() or "").strip()
                                if opt_text:
                                    option_texts.append(opt_text)
                        elif tag == "textarea":
                            field_type = "textarea"
                            option_texts = []
                        else:
                            option_texts = []

                        required = await field_el.get_attribute("required") is not None

                        fields.append(FormField(
                            name=field_name,
                            field_type=field_type,
                            label=label_text,
                            selector=f"#{field_id}" if field_id else f"[name='{field_name}']",
                            required=required,
                            options=option_texts,
                        ))
                    except Exception:
                        continue

                # 查找提交按钮
                submit_btn = await form_el.query_selector(
                    "button[type='submit'], input[type='submit'], button:not([type])"
                )
                submit_selector = ""
                if submit_btn:
                    btn_id = await submit_btn.get_attribute("id")
                    if btn_id:
                        submit_selector = f"#{btn_id}"

                forms.append(FormInfo(
                    form_id=form_id,
                    action=action,
                    method=method,
                    fields=fields,
                    submit_button=submit_selector,
                ))
            except Exception:
                continue

        return forms

    async def _extract_navigation(self, page) -> list[NavLink]:
        """提取导航链接"""
        nav_links = []
        links = await page.query_selector_all(
            "nav a, .sidebar a, .menu a, header a, "
            "footer a, [role='navigation'] a, .breadcrumb a, "
            ".nav a, .navbar a, .drawer a"
        )

        for link in links:
            try:
                text = (await link.text_content() or "").strip()
                href = await link.get_attribute("href") or ""
                if text and href and not href.startswith("#") and not href.startswith("javascript"):
                    nav_links.append(NavLink(
                        text=text[:50],
                        url=href,
                    ))
            except Exception:
                continue

        return nav_links

    async def _extract_links(self, page, base_url: str) -> list[str]:
        """提取页面中同域名的链接"""
        links = set()
        anchors = await page.query_selector_all("a[href]")
        for anchor in anchors:
            try:
                href = await anchor.get_attribute("href")
                if href:
                    full_url = urljoin(page.url, href)
                    if full_url.startswith(base_url) and "#" not in full_url:
                        links.add(full_url.split("?")[0])  # 去掉查询参数
            except Exception:
                continue
        return list(links)

    async def _detect_page_type(self, page, elements, forms) -> str:
        """检测页面类型"""
        url = page.url.lower()

        if "login" in url or "signin" in url:
            return "login"
        if "dashboard" in url or "home" in url:
            return "dashboard"
        if forms:
            return "form"
        # 检查是否有表格
        tables = await page.query_selector_all("table")
        if tables:
            return "list"
        if "detail" in url or "view" in url:
            return "detail"

        return "other"

    async def _parse_browser_use_result(
        self,
        result: Any,
        site: SiteKnowledge,
        base_url: str,
    ):
        """解析 Browser Use Agent 返回的结果"""
        result_str = str(result)

        # 尝试从结果中提取 JSON 数据
        try:
            # 查找 JSON 块
            json_start = result_str.find("{")
            json_end = result_str.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = result_str[json_start:json_end]
                data = json.loads(json_str)

                if "pages" in data:
                    for page_data in data["pages"]:
                        page = PageKnowledge.from_dict(page_data)
                        site.add_page(page)
                elif "url" in data:
                    page = PageKnowledge.from_dict(data)
                    site.add_page(page)
        except (json.JSONDecodeError, Exception) as e:
            logger.debug(f"解析 Browser Use 结果时跳过JSON: {e}")

        # 提取站点名称
        if not site.site_name and result_str:
            site.site_name = result_str[:100]

    async def _discover_sitemap_urls(self, page, base_url: str) -> list[str]:
        """从 sitemap.xml 和 robots.txt 自动发现额外的 URL"""
        import re as _re
        discovered = set()

        # 1. 尝试 /sitemap.xml
        try:
            response = await page.goto(f"{base_url}/sitemap.xml", wait_until="domcontentloaded", timeout=8000)
            if response and response.ok:
                content = await page.content()
                # 提取 <loc>...</loc> 中的 URL
                locs = _re.findall(r"<loc>\s*(.*?)\s*</loc>", content)
                for loc in locs:
                    if loc.startswith(base_url):
                        discovered.add(loc.split("?")[0])
                logger.info(f"sitemap.xml: 发现 {len(locs)} 个URL")
        except Exception as e:
            logger.debug(f"sitemap.xml 不可用: {e}")

        # 2. 尝试 /robots.txt，提取 Sitemap 和 Allow 路径
        try:
            response = await page.goto(f"{base_url}/robots.txt", wait_until="domcontentloaded", timeout=8000)
            if response and response.ok:
                content = await page.content()
                # 提取 Sitemap: 行
                sitemaps = _re.findall(r"Sitemap:\s*(\S+)", content, _re.IGNORECASE)
                for sm_url in sitemaps:
                    if sm_url.startswith(base_url):
                        discovered.add(sm_url)
                # 提取 Allow: 行
                allows = _re.findall(r"Allow:\s*(\S+)", content, _re.IGNORECASE)
                for path in allows:
                    if path.startswith("/"):
                        discovered.add(f"{base_url}{path}")
                logger.info(f"robots.txt: 发现 {len(sitemaps)} 个Sitemap + {len(allows)} 个Allow路径")
        except Exception as e:
            logger.debug(f"robots.txt 不可用: {e}")

        # 最多返回 30 个，避免爆炸
        return list(discovered)[:30]

    async def scan_single_page(self, url: str) -> PageKnowledge:
        """扫描单个页面（快速模式）"""
        from playwright.async_api import async_playwright

        print_agent("explorer", f"快速扫描页面: {url}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.browser.headless)
            page = await browser.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                from webagent.agents.vision_engine import VisionEngine
                await VisionEngine._wait_stable(page)
                knowledge = await self._extract_page_knowledge(page, url)
                print_success(f"页面扫描完成: {knowledge.title}")
                return knowledge
            finally:
                await browser.close()
