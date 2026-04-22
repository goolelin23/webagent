"""
SiteExplorer — Web 系统自主全量探索智能体

核心设计哲学：把 Web 系统探索建模为"状态空间搜索（State Space Search）"

  页面状态 = URL路径 + 当前可见 DOM 摘要
  动作      = 点击一个可交互元素
  目标      = 访问所有可达状态，穷举所有动作

算法: BFS（宽度优先搜索）with snapshot backtrack

  与 ActiveLearner（目标导向）的核心区别:
  - SiteExplorer 没有任务目标，只有"把每个角落都走一遍"的冲动
  - 每次操作后必定回退（restore），这样探索才能系统化
  - 发现的新状态加入全局 BFS 队列，按深度排序依次展开

主要流程:
  explore_queue: [(url, depth)]
  while queue not empty:
    1. 导航到目标 URL
    2. 截图 + VLM 一次性识别所有可交互元素
    3. for each 元素:
         save_snapshot -> click -> observe -> if new_state: enqueue URL -> restore
    4. 节点信息写入知识图谱
    5. 定期持久化（每3个节点保存一次）

产出:
  exploration_output/
    ├── site_graph.json       完整的系统状态图
    ├── site_map.md           易读的系统地图报告
    └── screenshots/          探索截图（支持 debug）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin

from playwright.async_api import Page

from webagent.agents.vision_engine import VisionEngine
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning, print_error

logger = get_logger("webagent.agents.site_explorer")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class DiscoveredElement:
    """探索过程中发现的一个可交互元素"""
    elem_id: str
    description: str
    elem_type: str          # button | link | input | select | tab | menu | other
    css_x: int
    css_y: int
    selector_hint: str = ""
    region_name: str = ""   # 所属逻辑区域（nav / sidebar / content...）
    priority: int = 3
    # 执行后的结果
    explored: bool = False
    outcome: str = ""       # page_changed | content_changed | dialog_appeared | same_page | error
    outcome_summary: str = ""
    led_to_url: str = ""


@dataclass
class SiteNode:
    """知识图谱中的一个节点（唯一 Web 页面状态）"""
    node_id: str
    url: str
    url_path: str           # 去掉 query/fragment 的路径
    title: str
    depth: int
    state_hash: str
    screenshot: str = ""    # 截图路径
    action_path: list[dict] = field(default_factory=list)
    elements: list[dict] = field(default_factory=list)
    elements_explored: int = 0
    elements_found: int = 0
    discovered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "url": self.url,
            "url_path": self.url_path,
            "title": self.title,
            "depth": self.depth,
            "state_hash": self.state_hash,
            "screenshot": self.screenshot,
            "action_path": self.action_path,
            "elements_found": self.elements_found,
            "elements_explored": self.elements_explored,
            "elements": self.elements,
            "discovered_at": self.discovered_at,
        }


@dataclass
class SiteEdge:
    """知识图谱中的一条边（一次交互操作）"""
    edge_id: str
    from_node: str
    to_node: str            # 空字符串=未跳转到新页面
    element_desc: str
    elem_type: str
    outcome: str
    summary: str
    new_url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VLM Prompts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DISCOVER_ELEMENTS_PROMPT = """你是一个专业的 Web 系统分析智能体。请仔细观察这张页面截图（截图中带有红色数字 SOM 标签标注了可识别的交互元素），识别出页面上所有可以与之交互的元素。

## 目标
全面识别所有可以被用户点击、填写、选择的元素，形成一张完整的"页面交互地图"。

## 区域感知
同时请对页面进行逻辑区域划分，将每个元素归属到对应区域：
- nav: 顶部导航栏、主菜单
- sidebar: 左侧或右侧导航菜单
- toolbar: 功能操作工具栏（新增、删除、导出等按钮）
- content: 主内容区（表格、卡片、表单、列表）
- breadcrumb: 面包屑导航
- footer: 底部区域

## 元素识别规则
1. 所有按钮（包括图标按钮）
2. 所有超链接（文字链接、图标链接）
3. 所有导航菜单项、标签页
4. 所有输入框、选择框（input、select、textarea）
5. 可展开/折叠的菜单项
6. 忽略: 禁用/灰色不可点击元素、纯装饰性元素
7. 安全过滤: 跳过"删除""注销""清空"等高危操作

## 输出要求
坐标 (x, y) 是元素中心在截图图像中的**像素坐标**（不是 CSS 坐标）。

返回 JSON（且仅返回 JSON）：
{{
    "page_title": "页面的功能标题",
    "page_type": "list | form | detail | dashboard | login | other",
    "elements": [
        {{
            "id": "SOM标签数字或auto_1、auto_2（若无标签）",
            "description": "元素文字内容或功能描述（中文，例如：用户管理菜单项）",
            "type": "button | link | input | select | tab | menu | checkbox | other",
            "region": "nav | sidebar | toolbar | content | breadcrumb | footer | other",
            "x": 元素中心在截图中的X像素坐标,
            "y": 元素中心在截图中的Y像素坐标,
            "selector_hint": "可推测的CSS选择器（可为空字符串）",
            "priority": 1到5的探索价值分（5最有价值，nav/sidebar菜单5分，content按钮4分，footer 1分）
        }}
    ]
}}

最多返回25个最有价值的元素，按 priority 从高到低排列。
"""

JUDGE_OUTCOME_PROMPT = """你是一个网页操作结果判断智能体。请对比这两张截图（操作前/后），快速判断操作产生了什么效果。

## 执行的操作
{action}

请返回 JSON（且仅返回 JSON）：
{{
    "outcome": "page_changed | content_changed | dialog_appeared | same_page | error",
    "summary": "一句话描述结果（20字以内，中文）",
    "new_url_hint": "若页面跳转，猜测目标页面的功能描述（可为空）"
}}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SiteExplorer 主体
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SiteExplorer:
    """
    Web 系统自主全量探索智能体

    给它一个 URL，它会：
    1. 打开页面，截图分析有哪些可以点/填/选的东西
    2. 逐一尝试每个元素，记录结果
    3. 发现新页面 → 加入队列 → 稍后探索
    4. 直到所有可达页面都探索完毕
    5. 输出完整的系统知识图谱 + Markdown 地图
    """

    def __init__(
        self,
        output_dir: str = "exploration_output",
        screenshots_dir: str = "screenshots/explorer",
    ):
        self.vision = VisionEngine(screenshots_dir=screenshots_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        Path(screenshots_dir).mkdir(parents=True, exist_ok=True)

        # 探索图谱
        self._nodes: dict[str, SiteNode] = {}   # hash → node
        self._edges: list[SiteEdge] = []
        self._visited: set[str] = set()          # 已访问的 state_hash
        self._node_counter = 0
        self._edge_counter = 0

        # BFS 队列: (url, depth, from_node_id, action_path)
        self._queue: deque[tuple[str, int, str, list[dict]]] = deque()
        # 已加入队列的 URL（去掉 query 的标准化路径，防重复入队）
        self._queued_urls: set[str] = set()
        # 已加入队列的状态哈希（防止同一状态重复入队）
        self._queued_hashes: set[str] = set()

        # 域名限制（探索时不跨域）
        self._allowed_origin: str = ""

    # ── 公共接口 ──────────────────────────────────────────

    async def explore(
        self,
        page: Page,
        max_depth: int = 3,
        max_nodes: int = 80,
        max_elements_per_page: int = 100,
        resume_path: str | None = None,
    ) -> dict:
        """
        主入口：从 page 当前 URL 开始，自主探索整个 Web 系统

        Args:
            page             已打开起始页面的 Playwright Page
            max_depth        最大跳转层数（深度上限）
            max_nodes        全局最多探索的页面状态数（防止无限扩展）
            max_elements_per_page  每个页面最多执行多少个元素的探索动作
            resume_path      断点续探：提供已有 site_graph.json 路径
        """
        start_url = page.url
        start_time = time.time()

        # 同域名限制
        parsed = urlparse(start_url)
        self._allowed_origin = f"{parsed.scheme}://{parsed.netloc}"

        if resume_path and Path(resume_path).exists():
            self._load_state(resume_path)
            print_agent("site_explorer", f"📂 断点续探，已恢复 {len(self._nodes)} 个节点")
        else:
            self._queue.append((start_url, 0, "", []))
            self._queued_urls.add(self._normalize_url(start_url))

        print_agent("site_explorer", f"\n🌐 Web 系统全量探索启动")
        print_agent("site_explorer", f"   起始地址: {start_url}")
        print_agent("site_explorer", f"   同域限制: {self._allowed_origin}")
        print_agent("site_explorer", f"   参数: max_depth={max_depth} | max_nodes={max_nodes} | elements/page={max_elements_per_page}")
        print_agent("site_explorer", "─" * 60)

        # ── BFS 主循环 ──
        while self._queue and len(self._nodes) < max_nodes:
            target_url, depth, from_node_id, action_path = self._queue.popleft()

            if depth > max_depth:
                continue

            # 导航到目标页面并重放记忆
            try:
                if page.url != target_url:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                await VisionEngine._wait_stable(page)

                # 下沉记忆：如果有 action_path，代表需要通过点击才能到达这个状态
                if action_path:
                    for act in action_path:
                        print_agent("site_explorer", f"  🧠 遵循记忆路径: 点击 {act.get('description', '未知')}")
                        await page.mouse.click(act["x"], act["y"])
                        await VisionEngine._wait_stable(page)
            except Exception as e:
                logger.warning(f"无法导航到 {target_url} 或执行记忆重放: {e}")
                continue

            # 检测是否为登录页面
            if await self._detect_login_page(page):
                print_agent("site_explorer", "\n[bold yellow]🔒 检测到需要用户登录的页面[/bold yellow]")
                from rich.prompt import Prompt
                account = Prompt.ask("  请输入登录账号 (留空跳过自主登录)")
                if account:
                    password = Prompt.ask("  请输入登录密码", password=True)
                    print_agent("site_explorer", "  🤖 正在自主帮用户操作登录，请稍候...")
                    goal = f"使用账号 '{account}' 和密码 '{password}' 登录系统。"
                    action_history = []
                    for step in range(6):
                        action = await self.vision.perceive(page, goal, action_history)
                        if action.is_dead_end:
                            break
                        action_desc = f"{action.action_type} -> {action.target_description}"
                        print_agent("site_explorer", f"    -> {action_desc}")
                        await self.vision.execute_vision_action(page, action)
                        await VisionEngine._wait_stable(page)
                        action_history.append(action_desc)
                        
                        # 检测是否登录完成（判断是否还是登录页面）
                        still_login = await self._detect_login_page(page)
                        if not still_login:
                            print_success("  ✅ 成功完成自主登录！继续探索...")
                            break
                    else:
                        print_warning("  ⚠️ 登录过程似乎未完全成功或仍在同一页面，继续探索...")

            # 去重检查（状态哈希）
            state_hash = await self._compute_hash(page)
            if state_hash in self._visited:
                print_agent("site_explorer", f"  ⟳ 已探索此状态，跳过: {page.url[:60]}")
                continue
            self._visited.add(state_hash)

            # 创建节点
            self._node_counter += 1
            node_id = f"N{self._node_counter:04d}"
            title = await self._get_title(page)

            print_agent(
                "site_explorer",
                f"\n{'━'*55}\n"
                f"[{node_id}] 深度={depth} | {title[:35]}\n"
                f"      {page.url[:70]}"
            )

            # 截图（带 SOM 标注）
            ss_path = await self.vision._screenshot(page, f"node_{node_id}", draw_som=True)

            # 用 VLM 一次性识别所有可交互元素
            elements = await self._discover_elements(page, ss_path)
            print_agent("site_explorer", f"  📋 发现 {len(elements)} 个可交互元素")

            # 建立节点
            node = SiteNode(
                node_id=node_id,
                url=page.url,
                url_path=self._normalize_url(page.url),
                title=title,
                depth=depth,
                state_hash=state_hash,
                screenshot=ss_path,
                action_path=action_path,
                elements_found=len(elements),
            )

            # 按区域 + 优先级排序（nav/sidebar 最先探索）
            region_order = {"nav": 0, "sidebar": 1, "toolbar": 2, "breadcrumb": 3, "content": 4, "footer": 5, "other": 6}
            elements_sorted = sorted(
                elements[:max_elements_per_page],
                key=lambda e: (region_order.get(e.region_name, 6), -e.priority)
            )

            # ── 生成最终综合 SOM 标签图 ──
            final_som_path = str(self.output_dir / f"final_som_N{self._node_counter:04d}.jpg")
            await self._draw_final_som_image(page, ss_path, elements, final_som_path)
            node.screenshot = final_som_path  # 用带全局标签的图替换原始快照路径

            # ── 逐元素探索（支持最大 100 次点击/页） ──
            for i, elem in enumerate(elements_sorted):
                print_agent(
                    "site_explorer",
                    f"  [{i+1:02d}/{len(elements_sorted):02d}] [{elem.region_name}/{elem.elem_type}] {elem.description[:45]}"
                )

                snapshot = await self._snapshot(page)
                url_before = page.url
                ss_before = await self.vision._screenshot(page, f"b_{node_id}_{i:02d}")

                # 执行点击（三阶精修）
                ok = await self._click_element(page, elem, ss_path)
                if not ok:
                    print_warning(f"    ❌ 点击失败")
                    elem.outcome = "error"
                    elem.explored = True
                    await self._restore(page, snapshot)
                    continue

                await VisionEngine._wait_stable(page)
                url_after = page.url
                new_hash = await self._compute_hash(page)
                ss_after = await self.vision._screenshot(page, f"a_{node_id}_{i:02d}")

                # 判断结果
                outcome_data = await self._judge_outcome(ss_before, ss_after, elem.description)
                outcome = outcome_data.get("outcome", "same_page")
                summary = outcome_data.get("summary", "")

                elem.outcome = outcome
                elem.outcome_summary = summary
                elem.explored = True
                elem.led_to_url = url_after if url_after != url_before else ""
                node.elements_explored += 1

                print_success(f"    ✅ {outcome}: {summary}")

                # 记录边
                self._edge_counter += 1
                edge = SiteEdge(
                    edge_id=f"E{self._edge_counter:05d}",
                    from_node=node_id,
                    to_node="",
                    element_desc=elem.description,
                    elem_type=elem.elem_type,
                    outcome=outcome,
                    summary=summary,
                    new_url=url_after if url_after != url_before else "",
                )

                # 发现新状态 → 判断是否入队
                if new_hash not in self._visited and new_hash not in getattr(self, '_queued_hashes', set()) and outcome in ("page_changed", "content_changed", "dialog_appeared"):
                    if not hasattr(self, '_queued_hashes'):
                        self._queued_hashes = set()
                    self._queued_hashes.add(new_hash)
                    
                    new_url = page.url
                    if self._is_same_origin(new_url):
                        # 记录行动轨迹
                        if new_url != url_before:
                            new_action_path = []
                        else:
                            new_action_path = action_path + [{"description": elem.description, "x": elem.css_x, "y": elem.css_y}]
                            
                        norm = self._normalize_url(new_url)
                        if new_url != url_before:
                            if norm not in self._queued_urls:
                                self._queued_urls.add(norm)
                                self._queue.append((new_url, depth + 1, node_id, new_action_path))
                                print_agent("site_explorer", f"    ➕ 新页面入队: {new_url[:60]} (depth={depth+1})")
                        else:
                            self._queue.append((new_url, depth + 1, node_id, new_action_path))
                            print_agent("site_explorer", f"    ➕ 页面新状态入队: {new_url[:60]} (depth={depth+1})")

                edge.to_node = new_url if url_after != url_before else ""
                self._edges.append(edge)

                # 用户要求：点击或操作一次就回退一次，先把首层页面探索完，不再立即递归探索展开项

                # 回退到点击前

                await self._restore(page, snapshot)

            # 节点存档
            node.elements = [self._elem_to_dict(e) for e in elements_sorted]
            self._nodes[state_hash] = node

            # 每 3 个节点自动保存一次
            if len(self._nodes) % 3 == 0:
                self._persist()
                print_agent("site_explorer", f"  💾 已自动保存进度（{len(self._nodes)} 节点）")

        # 最终保存
        self._persist()
        self._generate_sitemap()

        duration = round(time.time() - start_time, 1)

        summary = {
            "start_url": start_url,
            "duration_seconds": duration,
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "total_elements_explored": sum(n.elements_explored for n in self._nodes.values()),
            "output_dir": str(self.output_dir),
        }

        print_success(
            f"\n{'='*60}\n"
            f"🎉 Web 系统探索完成！\n"
            f"  ⏱️  耗时:         {duration}s\n"
            f"  📄 探索页面数:   {len(self._nodes)}\n"
            f"  🔀 操作路径数:   {len(self._edges)}\n"
            f"  🎯 已探索元素:   {summary['total_elements_explored']}\n"
            f"  📁 结果保存于:   {self.output_dir}/\n"
            f"{'='*60}"
        )
        return summary

    # ── 元素发现 ──────────────────────────────────────────

    async def _discover_elements(self, page: Page, screenshot_path: str) -> list[DiscoveredElement]:
        """
        双轨融合元素发现 — 保证尽量完整识别页面所有可交互元素

        两条轨道:
        Track A: DOM 精确枚举     用 querySelectorAll 从 DOM 中找到所有真实可点击元素
                                  -> 坐标 100% 准确，不依赖 VLM 猜测
        Track B: VLM 语义标注     在 SOM 截图中识别，补充 DOM 可能漏掉的动态/Canvas 元素
                                  -> 描述更好，区域感知更准

        滚动覆盖:
        对超过视口高度的页面，分 viewport 高度段截图识别，确保折叠区域被扫描

        融合策略: DOM 元素为基础（保证 Y 坐标精确），VLM 结果去重补充
        """
        from PIL import Image

        vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight, scrollH: document.body.scrollHeight})")
        vw, vh = int(vp["w"]), int(vp["h"])
        scroll_h = int(vp.get("scrollH", vh))

        all_elements: list[DiscoveredElement] = []
        fingerprints: set[str] = set()  # 去重指纹（描述+坐标）

        # ─────────────────────────────────────────
        # Track A: DOM 精确枚举（滚动全页面）
        # ─────────────────────────────────────────
        try:
            dom_elements = await page.evaluate("""
            () => {
                const SKIP_KEYWORDS = ['退出', '注销', '删除', 'logout', 'delete', 'remove', 'sign out'];
                const results = [];
                const seen = new Set();
                
                // 抓取所有潜在可交互元素
                const selectors = [
                    'a[href]',
                    'button:not([disabled])',
                    'input:not([disabled]):not([type="hidden"])',
                    'select:not([disabled])',
                    'textarea:not([disabled])',
                    '[role="button"]:not([disabled])',
                    '[role="tab"]',
                    '[role="menuitem"]',
                    '[role="option"]',
                    '[onclick]',
                    '[data-action]',
                    '[data-href]',
                    'label[for]',
                ];
                
                const allEls = document.querySelectorAll(selectors.join(','));
                
                for (const el of allEls) {
                    const rect = el.getBoundingClientRect();
                    // 过滤不可见 / 尺寸太小的元素
                    if (rect.width < 4 || rect.height < 4) continue;
                    if (el.offsetParent === null && el.tagName !== 'BODY') continue;
                    
                    // 计算在整个文档中的绝对坐标（不是视口相对）
                    const absX = Math.round(rect.left + rect.width / 2);
                    const absY = Math.round(rect.top + window.scrollY + rect.height / 2);
                    
                    const key = `${absX}|${absY}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    
                    // 提取描述文字
                    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt') || el.tagName).trim().substring(0, 60);
                    
                    // 过滤危险操作
                    if (SKIP_KEYWORDS.some(k => text.toLowerCase().includes(k))) continue;
                    
                    // 判断元素类型
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    let elemType = 'other';
                    if (tag === 'a') elemType = 'link';
                    else if (tag === 'button' || role === 'button') elemType = 'button';
                    else if (tag === 'input') elemType = 'input';
                    else if (tag === 'select') elemType = 'select';
                    else if (tag === 'textarea') elemType = 'input';
                    else if (role === 'tab') elemType = 'tab';
                    else if (role === 'menuitem' || role === 'option') elemType = 'menu';
                    
                    // 判断所属区域（简单启发式）
                    let region = 'content';
                    const headersAndNavs = el.closest('header, nav, [role="navigation"]');
                    const sidebar = el.closest('aside, [role="complementary"], .sidebar, .side-menu, .nav-sidebar');
                    const toolbar = el.closest('[class*="toolbar"], [class*="tool-bar"], [class*="action-bar"]');
                    const footer = el.closest('footer, [role="contentinfo"]');
                    if (headersAndNavs) region = 'nav';
                    else if (sidebar) region = 'sidebar';
                    else if (toolbar) region = 'toolbar';
                    else if (footer) region = 'footer';
                    
                    results.push({
                        text,
                        elemType,
                        region,
                        // css_x 是视口坐标（相对当前滚动位置）
                        css_x: Math.round(rect.left + rect.width / 2),
                        css_y: Math.round(rect.top + rect.height / 2),  // 视口坐标
                        abs_y: absY,  // 文档绝对 Y
                        tagName: el.tagName,
                    });
                }
                return results;
            }
            """)

            for raw in dom_elements:
                cx, cy = int(raw.get("css_x", 0)), int(raw.get("css_y", 0))
                abs_y = int(raw.get("abs_y", cy))
                if cx <= 0:
                    continue
                desc = raw.get("text", "")[:50] or f"{raw.get('tagName','?')} @ {cx},{abs_y}"
                fp = f"{desc[:20]}_{cx}_{cy}"
                if fp in fingerprints:
                    continue
                fingerprints.add(fp)
                all_elements.append(DiscoveredElement(
                    elem_id=f"dom_{len(all_elements)+1}",
                    description=desc,
                    elem_type=raw.get("elemType", "other"),
                    css_x=max(0, min(vw, cx)),
                    css_y=max(0, min(vh, cy)),
                    region_name=raw.get("region", "content"),
                    priority=5 if raw.get("region") in ("nav", "sidebar") else 3,
                ))

            logger.debug(f"DOM Track: 找到 {len(all_elements)} 个元素")
        except Exception as e:
            logger.warning(f"DOM 枚举失败: {e}")

        # ─────────────────────────────────────────
        # Track B: VLM 语义识别（包含滚动扫描）
        # ─────────────────────────────────────────
        # 分段滚动截图：确保页面折叠区域也被扫描
        scroll_positions = [0]  # 从顶部开始
        overlap = int(vh * 0.15)  # 重叠区域，避免漏掉边界元素
        step = vh - overlap
        pos = step
        while pos < scroll_h and len(scroll_positions) < 4:  # 最多滚4屏
            scroll_positions.append(pos)
            pos += step

        for scroll_y in scroll_positions:
            try:
                if scroll_y > 0:
                    await page.evaluate(f"window.scrollTo(0, {scroll_y})")
                    await asyncio.sleep(0.3)

                seg_ss = await self.vision._screenshot(page, f"scroll_{scroll_y}_{int(time.time())}", draw_som=True)

                response = await self.vision._call_vision_llm(DISCOVER_ELEMENTS_PROMPT, [seg_ss])
                data = self.vision._extract_json(response)
                if not data or "elements" not in data:
                    continue

                with Image.open(seg_ss) as img:
                    img_w, img_h = img.size

                for raw in data["elements"]:
                    img_x = int(raw.get("x", 0))
                    img_y = int(raw.get("y", 0))
                    if img_x <= 0 and img_y <= 0:
                        continue
                    # 坐标换算 + 加回滚动偏移
                    css_x, css_y_viewport = await self.vision._scale_llm_coords(page, img_x, img_y, seg_ss)
                    css_y_doc = css_y_viewport + scroll_y  # 换算为文档坐标
                    # 使用视口坐标用于点击（需在视口内）
                    click_y = max(0, min(vh, css_y_viewport))
                    desc = raw.get("description", "")[:50]
                    fp = f"{desc[:20]}_{css_x}_{click_y}"
                    if fp in fingerprints:
                        continue
                    fingerprints.add(fp)
                    all_elements.append(DiscoveredElement(
                        elem_id=str(raw.get("id", f"vlm_{len(all_elements)+1}")),
                        description=desc,
                        elem_type=raw.get("type", "other"),
                        css_x=max(0, min(vw, css_x)),
                        css_y=click_y,
                        selector_hint=raw.get("selector_hint", ""),
                        region_name=raw.get("region", "other"),
                        priority=int(raw.get("priority", 3)),
                    ))
            except Exception as e:
                logger.debug(f"VLM 滚动段扫描失败 (y={scroll_y}): {e}")

        # 滚回顶部
        await page.evaluate("window.scrollTo(0, 0)")

        logger.debug(f"双轨融合: 共发现 {len(all_elements)} 个可交互元素")
        return all_elements


    async def _draw_final_som_image(self, page: Page, screenshot_path: str, elements: list[DiscoveredElement], output_path: str):
        """
        最后用 SOM 打好标签:
        将探索发现的全部 100+ 个可交互元素物理绘制到截图中，生成带有红色数字标签的最终全景探索图
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            import os

            if not os.path.exists(screenshot_path):
                return

            with Image.open(screenshot_path) as img:
                img = img.convert("RGBA")
                draw = ImageDraw.Draw(img)
                
                # 读取视口信息和 DPR
                vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio})")
                dpr = float(vp.get("dpr", 1.0))
                scroll_y = await page.evaluate("window.scrollY")

                # 尝试加载字体，如果不存在则使用默认
                try:
                    font = ImageFont.truetype("Arial", size=16)
                except IOError:
                    font = ImageFont.load_default()

                for i, elem in enumerate(elements):
                    # 把 CSS 逻辑坐标算回截图物理坐标
                    # 截图如果是全页长图，或者只是当前视口截屏
                    # css_x, css_y 在这里我们目前记录的是相对视口的或者绝对的
                    # 为了简化，直接转换
                    px = int(elem.css_x * dpr)
                    py = int(elem.css_y * dpr)
                    
                    # 绘制方块和小红框
                    box_w, box_h = 24, 24
                    left, top = px - box_w // 2, py - box_h // 2
                    
                    # 画红框
                    draw.rectangle([left-2, top-2, left+box_w+2, top+box_h+2], outline="red", width=2)
                    # 画底色块
                    draw.rectangle([left, top, left+box_w, top+box_h], fill="red")
                    
                    # 居中画数字标签 (这里直接用 i+1 作为 SOM ID，对应 elements_sorted 里的顺序)
                    text = str(i + 1)
                    # get text size
                    # try fallback text length approximation if textbbox not strictly uniform
                    draw.text((left + 4, top + 4), text, fill="white", font=font)

                # 将图片保存为 RGB 格式 jpg，减小体积
                img.convert("RGB").save(output_path, "JPEG", quality=85)
                logger.debug(f"已生成最终全页 SOM 标记图: {output_path} (打上了 {len(elements)} 个标签)")

        except Exception as e:
            logger.debug(f"绘制最终 SOM 标签图失败: {e}")
            import shutil
            shutil.copy2(screenshot_path, output_path)

    # ── 点击执行 ──────────────────────────────────────────

    async def _click_element(self, page: Page, elem: DiscoveredElement, screenshot_path: str) -> bool:
        """
        带验证重试的元素点击（针对 nav/sidebar 导航栏强化）

        策略:
          1. 对 nav/sidebar/toolbar 区域的元素，先 hover 再 click（很多导航菜单需要 hover 激活）
          2. 点击后验证是否生效（URL 或 DOM hash 是否变化）
          3. 如果第一次没生效 → 最多重试 2 次，每次使用不同的降级策略:
             - 重试1: 直接用 selector 精确点击（绕过坐标）
             - 重试2: 坐标微偏移 + 双击兜底
        """
        from webagent.agents.vision_engine import VisionAction
        try:
            x, y = elem.css_x, elem.css_y

            # 二阶精修
            rx, ry, zoom_conf = await self.vision._zoom_refine_coords(
                page, x, y, elem.description, zoom_radius=180
            )
            if zoom_conf >= 0.40:
                x, y = rx, ry

            # DOM 吸附
            fx, fy, sel, _ = await self.vision._refine_coordinates(page, x, y, elem.description)
            final_selector = elem.selector_hint or sel

            # 判断是否属于高风险区域（nav/sidebar/toolbar 元素小而密集，容易点偏）
            is_nav_region = elem.region_name in ("nav", "sidebar", "toolbar", "breadcrumb")

            # ── 策略1: hover-before-click（nav 区域专属） ──
            # 很多导航菜单第一次点击只触发 hover 态（如 dropdown），需要先 hover 激活再点击
            if is_nav_region:
                try:
                    await page.mouse.move(fx, fy)
                    await asyncio.sleep(0.25)  # 等待 hover 态动画/展开
                except Exception:
                    pass

            # 记录点击前的状态用于验证
            url_before = page.url
            hash_before = await self._compute_hash(page)

            # ── 首次点击 ──
            action = VisionAction(
                action_type="click",
                target_description=elem.description,
                coordinates={"x": fx, "y": fy},
                element_id=elem.elem_id,
                selector_hint=final_selector,
            )
            click_ok = await self.vision.execute_vision_action(page, action)
            if not click_ok:
                return False

            await VisionEngine._wait_stable(page)

            # ── 验证点击是否生效 ──
            url_after = page.url
            hash_after = await self._compute_hash(page)
            click_had_effect = (url_after != url_before) or (hash_after != hash_before)

            if click_had_effect:
                return True  # 首次点击就生效了

            # ── 对 nav 区域执行智能重试（最多 2 次） ──
            if not is_nav_region:
                return True  # 非导航区域不做重试，避免误操作

            max_retries = 2
            for retry in range(max_retries):
                retry_strategy = ""

                if retry == 0 and final_selector:
                    # ── 重试1: selector 精确点击（绕过坐标精度问题） ──
                    retry_strategy = "selector直接点击"
                    try:
                        locator = page.locator(final_selector)
                        if await locator.count() > 0:
                            await locator.first.hover(timeout=2000)
                            await asyncio.sleep(0.2)
                            await locator.first.click(timeout=3000)
                            await VisionEngine._wait_stable(page)
                        else:
                            continue
                    except Exception:
                        continue

                elif retry == 0 and not final_selector:
                    # ── 重试1(无selector): hover + 再次坐标点击 ──
                    retry_strategy = "hover+重新点击"
                    try:
                        await page.mouse.move(fx, fy)
                        await asyncio.sleep(0.3)
                        await page.mouse.click(fx, fy)
                        await VisionEngine._wait_stable(page)
                    except Exception:
                        continue

                elif retry == 1:
                    # ── 重试2: 坐标微偏移(±3px扫射) + 尝试描述文本匹配 ──
                    retry_strategy = "文本匹配+偏移扫射"
                    clicked = False
                    # 先尝试通过文本内容匹配元素
                    if elem.description:
                        try:
                            desc_text = elem.description.strip()[:20]
                            # 尝试通过可见文本精确匹配
                            text_locator = page.get_by_text(desc_text, exact=False)
                            if await text_locator.count() > 0:
                                await text_locator.first.click(timeout=3000)
                                await VisionEngine._wait_stable(page)
                                clicked = True
                        except Exception:
                            pass
                    # 文本匹配失败则做偏移扫射
                    if not clicked:
                        try:
                            for dx, dy in [(0, -3), (0, 3), (-3, 0), (3, 0)]:
                                await page.mouse.click(fx + dx, fy + dy)
                                await asyncio.sleep(0.15)
                            await VisionEngine._wait_stable(page)
                        except Exception:
                            continue

                # 验证重试结果
                hash_now = await self._compute_hash(page)
                url_now = page.url
                if url_now != url_before or hash_now != hash_before:
                    logger.debug(f"    🔄 nav 重试({retry_strategy})成功, 第{retry+2}次点击生效")
                    return True
                logger.debug(f"    🔄 nav 重试({retry_strategy})未生效, 继续降级...")

            # 所有重试均未生效，仍返回 True（点击本身成功了，只是页面没变化，可能本就是当前页面）
            logger.debug(f"    ⚠️ nav 元素 '{elem.description[:25]}' 点击{max_retries+1}次均无变化")
            return True

        except Exception as e:
            logger.debug(f"点击异常: {e}")
            return False

    async def _scan_expanded_elements(self, page: Page, original_element_descs: set[str]) -> list[DiscoveredElement]:
        """
        展开后补扫 — 当一次点击展开了下拉菜单/手风琴时，
        立即重新扫描 DOM 找到新冒出的可见元素，返回尚未见过的新元素
        """
        try:
            vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            vw, vh = int(vp["w"]), int(vp["h"])
            new_dom = await page.evaluate("""
            () => {
                const results = [];
                const els = document.querySelectorAll(
                    'a[href], button:not([disabled]), [role="menuitem"], [role="option"], [role="treeitem"]'
                );
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    // 只抓当前在视口内可见的元素
                    if (rect.top < 0 || rect.bottom > window.innerHeight) continue;
                    if (rect.width < 4 || rect.height < 4) continue;
                    const text = (el.innerText || el.getAttribute('aria-label') || '').trim().substring(0, 50);
                    if (!text) continue;
                    results.push({
                        text,
                        css_x: Math.round(rect.left + rect.width/2),
                        css_y: Math.round(rect.top + rect.height/2),
                    });
                }
                return results;
            }
            """)
            result = []
            for raw in new_dom:
                desc = raw.get("text", "")
                if not desc or desc in original_element_descs:
                    continue
                cx, cy = int(raw.get("css_x", 0)), int(raw.get("css_y", 0))
                result.append(DiscoveredElement(
                    elem_id=f"expanded_{len(result)+1}",
                    description=desc,
                    elem_type="menu",
                    css_x=max(0, min(vw, cx)),
                    css_y=max(0, min(vh, cy)),
                    region_name="sidebar",
                    priority=5,  # 展开的子菜单优先级最高
                ))
            return result
        except Exception as e:
            logger.debug(f"展开补扫失败: {e}")
            return []

    # ── 结果判断 ──────────────────────────────────────────

    async def _judge_outcome(self, ss_before: str, ss_after: str, action: str) -> dict:
        """VLM 对比前后截图判断操作结果"""
        try:
            prompt = JUDGE_OUTCOME_PROMPT.format(action=action)
            response = await self.vision._call_vision_llm(prompt, [ss_before, ss_after])
            return self.vision._extract_json(response) or {"outcome": "same_page", "summary": ""}
        except Exception:
            return {"outcome": "same_page", "summary": ""}

    # ── 快照 / 回退 ───────────────────────────────────────

    async def _snapshot(self, page: Page) -> dict:
        """记录当前完整页面状态快照，用于后续精确回退"""
        # 检测当前是否存在打开的弹窗/对话框/抽屉
        has_dialog = False
        try:
            has_dialog = await page.evaluate("""
                () => {
                    // 检测常见框架的 Modal/Dialog/Drawer 打开状态
                    const modalSelectors = [
                        '[role="dialog"]:not([hidden])',
                        '[role="alertdialog"]:not([hidden])',
                        '.el-dialog__wrapper:not([style*="display: none"])',   // ElementUI
                        '.ant-modal-wrap:not(.ant-modal-wrap-hidden)',          // Ant Design
                        '.v-dialog--active',                                   // Vuetify
                        '.modal.show',                                         // Bootstrap
                        '[class*="drawer"][class*="open"]',
                        '[class*="modal"][class*="open"]',
                    ];
                    return modalSelectors.some(sel => document.querySelector(sel) !== null);
                }
            """)
        except Exception:
            pass

        # 记录历史栈长度，用于判断 go_back 是否可用
        history_len = 0
        try:
            history_len = await page.evaluate("window.history.length")
        except Exception:
            pass

        return {
            "url": page.url,
            "sx": await page.evaluate("window.scrollX"),
            "sy": await page.evaluate("window.scrollY"),
            "hash": await self._compute_hash(page),
            "has_dialog": has_dialog,
            "history_len": history_len,
        }

    async def _restore(self, page: Page, snapshot: dict):
        """
        多级回退策略（六层降级，按速度从快到慢）

        策略优先级:
          L1  Escape键         — 最快，专门关闭弹窗/对话框/抽屉（URL不变的DOM突变）
          L2  page.go_back()  — 快，浏览器原生历史栈后退（URL改变的跳转）
          L3  history.back()  — JS 层 go_back，绕过 Playwright 包装，作为 L2 的补充
          L4  page.reload()   — 中速，SPA 状态污染时强制刷新回初始状态
          L5  page.goto(url)  — 慢，URL 不同但 go_back 系列均失败时精确导航
          L6  放弃            — 所有策略均失败时记录警告，继续探索

        每层策略执行后，用 state_hash 验证是否已成功恢复，
        验证通过则立即停止降级，避免不必要的慢操作。
        """
        target_url = snapshot["url"]
        target_hash = snapshot.get("hash", "")

        async def _hash_matches() -> bool:
            """验证当前状态是否与快照一致"""
            if not target_hash:
                return page.url == target_url
            return (await self._compute_hash(page)) == target_hash

        # ── L1: Escape键 — 专门处理「URL 没变，弹窗/抽屉被打开」的情况 ──
        # 适合：Modal、Drawer、Dropdown、Tooltip 等覆盖层
        if snapshot.get("has_dialog") is False and page.url == target_url:
            # 快速检测：当前是否冒出了新弹窗（快照中没有但现在有）
            try:
                now_has_dialog = await page.evaluate("""
                    () => {
                        const modalSelectors = [
                            '[role="dialog"]:not([hidden])',
                            '[role="alertdialog"]:not([hidden])',
                            '.el-dialog__wrapper:not([style*="display: none"])',
                            '.ant-modal-wrap:not(.ant-modal-wrap-hidden)',
                            '.v-dialog--active',
                            '.modal.show',
                        ];
                        return modalSelectors.some(sel => document.querySelector(sel) !== null);
                    }
                """)
                if now_has_dialog:
                    await page.keyboard.press("Escape")
                    await VisionEngine._wait_stable(page)
                    logger.debug("  ⌨️  L1: Escape 关闭弹窗")
                    if await _hash_matches():
                        logger.debug("  ✅ L1 Escape 回退成功")
                        await self._restore_scroll(page, snapshot)
                        return
            except Exception as e:
                logger.debug(f"  L1 Escape 检测失败: {e}")

        # ── L2: page.go_back() — URL 发生变化时的首选快速回退 ──
        # 依赖浏览器 History 栈，支持 BFCache 瞬间恢复，无需重新渲染
        if page.url != target_url and snapshot.get("history_len", 0) > 1:
            try:
                result = await page.go_back(wait_until="domcontentloaded", timeout=6000)
                if result is not None:  # go_back 成功返回 Response（导航成功）
                    await VisionEngine._wait_stable(page)
                    logger.debug("  🔙 L2: go_back() 执行完成")
                    if await _hash_matches():
                        logger.debug("  ✅ L2 go_back 回退成功")
                        await self._restore_scroll(page, snapshot)
                        return
            except Exception as e:
                logger.debug(f"  L2 go_back 失败: {e}")

        # ── L3: history.back() — JS 层 go_back，绕过 Playwright 网络层包装 ──
        # 适合：SPA 自己管理 History 但 Playwright go_back 无法捕获的情况
        if page.url != target_url:
            try:
                await page.evaluate("window.history.back()")
                await VisionEngine._wait_stable(page)
                logger.debug("  🔙 L3: history.back() 执行完成")
                if await _hash_matches():
                    logger.debug("  ✅ L3 history.back 回退成功")
                    await self._restore_scroll(page, snapshot)
                    return
            except Exception as e:
                logger.debug(f"  L3 history.back 失败: {e}")

        # ── L4: page.reload() — URL 未变但 DOM 状态被污染（SPA 内部状态机混乱）──
        # 适合：展开了侧边栏子菜单但无 URL 变化，VLM 判断 content_changed 的场景
        if page.url == target_url:
            current_hash = await self._compute_hash(page)
            if current_hash != target_hash:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=12000)
                    await VisionEngine._wait_stable(page)
                    logger.debug("  🔄 L4: reload() 清理 SPA DOM 污染")
                    if await _hash_matches():
                        logger.debug("  ✅ L4 reload 回退成功")
                        await self._restore_scroll(page, snapshot)
                        return
                except Exception as e:
                    logger.debug(f"  L4 reload 失败: {e}")

        # ── L5: page.goto(url) — 终极保底，精确导航到目标 URL ──
        # 最慢但最可靠，用于 go_back 系列全部失败后的兜底
        if page.url != target_url or not await _hash_matches():
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                await VisionEngine._wait_stable(page)
                logger.debug(f"  🌐 L5: goto({target_url[:60]}) 精确导航")
                if await _hash_matches():
                    logger.debug("  ✅ L5 goto 回退成功")
                else:
                    logger.debug("  ⚠️  L5 goto 后 hash 仍不匹配（页面动态内容，可接受）")
                await self._restore_scroll(page, snapshot)
                return
            except Exception as e:
                logger.warning(f"  ❌ L5 goto 回退失败: {e}")

        # ── L6: 放弃（所有策略均失败）──
        logger.warning(f"  ⚠️  所有回退策略均失败，当前 URL={page.url[:60]}，继续探索")

    async def _restore_scroll(self, page: Page, snapshot: dict):
        """恢复滚动位置（回退后的收尾步骤）"""
        try:
            await page.evaluate(f"window.scrollTo({snapshot.get('sx', 0)}, {snapshot.get('sy', 0)})")
        except Exception:
            pass
        await asyncio.sleep(0.1)

    # ── 状态哈希 / URL 工具 ───────────────────────────────

    async def _compute_hash(self, page: Page) -> str:
        """URL路径 + DOM关键内容 → 状态哈希"""
        try:
            url_key = self._normalize_url(page.url)
            dom = await page.evaluate("""() => {
                const b = document.body;
                if (!b) return '';
                // 取标题 + 前500字可见内容
                const title = document.title || '';
                const text = b.innerText.substring(0, 500).replace(/\\s+/g, ' ').trim();
                return title + '||' + text;
            }""")
            return hashlib.md5(f"{url_key}||{dom}".encode()).hexdigest()[:16]
        except Exception:
            return hashlib.md5(page.url.encode()).hexdigest()[:16]

    def _normalize_url(self, url: str) -> str:
        """标准化 URL：去除末尾斜杠，保留 query 和 fragment"""
        try:
            p = urlparse(url)
            # 保持 query 和 fragment，因为很多 SPA 依赖它们进行路由
            path = p.path.rstrip('/') or '/'
            return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))
        except Exception:
            return url

    def _is_same_origin(self, url: str) -> bool:
        """判断 URL 是否在同一源站"""
        try:
            return url.startswith(self._allowed_origin)
        except Exception:
            return False

    async def _get_title(self, page: Page) -> str:
        try:
            t = await page.title()
            return t.strip() or page.url.split("/")[-1] or "untitled"
        except Exception:
            return "untitled"

    async def _detect_login_page(self, page: Page) -> bool:
        """检测当前页面是否为登录页面"""
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

    # ── 序列化 ───────────────────────────────────────────

    @staticmethod
    def _elem_to_dict(e: DiscoveredElement) -> dict:
        return {
            "id": e.elem_id,
            "description": e.description,
            "type": e.elem_type,
            "region": e.region_name,
            "x": e.css_x,
            "y": e.css_y,
            "priority": e.priority,
            "explored": e.explored,
            "outcome": e.outcome,
            "summary": e.outcome_summary,
            "led_to": e.led_to_url,
        }

    # ── 持久化 ───────────────────────────────────────────

    def _persist(self):
        """保存探索图谱 JSON（支持断点续探）"""
        data = {
            "visited_hashes": list(self._visited),
            "queued_urls": list(self._queued_urls),
            "queued_hashes": list(getattr(self, '_queued_hashes', set())),
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges],
            "stats": {
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
            },
            "updated_at": time.time(),
        }
        path = self.output_dir / "site_graph.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_state(self, path: str):
        """从已有 JSON 恢复状态（断点续探）"""
        data = json.loads(Path(path).read_text())
        self._visited = set(data.get("visited_hashes", []))
        self._queued_urls = set(data.get("queued_urls", []))
        self._queued_hashes = set(data.get("queued_hashes", []))
        for n in data.get("nodes", []):
            node = SiteNode(
                node_id=n["node_id"], url=n["url"], url_path=n.get("url_path", ""),
                title=n["title"], depth=n["depth"], state_hash=n["state_hash"],
                screenshot=n.get("screenshot", ""), action_path=n.get("action_path", []),
                elements=n.get("elements", []),
                elements_found=n.get("elements_found", 0),
                elements_explored=n.get("elements_explored", 0),
            )
            self._nodes[n["state_hash"]] = node
        for e in data.get("edges", []):
            self._edges.append(SiteEdge(**e))
        self._node_counter = len(self._nodes)
        self._edge_counter = len(self._edges)
        # 恢复队列（之前入队但未访问的 URL）
        for url in self._queued_urls:
            if not any(url == self._normalize_url(n.url) for n in self._nodes.values()):
                self._queue.append((url, 0, "", []))

    # ── 报告生成 ─────────────────────────────────────────

    def _generate_sitemap(self):
        """生成易读的 Markdown 站点地图报告"""
        nodes = list(self._nodes.values())
        nodes.sort(key=lambda n: (n.depth, n.node_id))

        lines = [
            "# 🌐 Web 系统探索报告 - Site Map",
            "",
            f"> 探索节点: {len(nodes)} | 操作记录: {len(self._edges)}",
            "",
            "## 📄 页面清单",
            "",
            "| 节点 | 深度 | 页面标题 | URL | 互动元素 | 已探索 |",
            "|------|------|---------|-----|---------|--------|",
        ]
        for n in nodes:
            url_short = n.url[:55] + "..." if len(n.url) > 55 else n.url
            lines.append(
                f"| {n.node_id} | {n.depth} | {n.title[:25]} | `{url_short}` "
                f"| {n.elements_found} | {n.elements_explored} |"
            )

        lines += ["", "## 🔀 操作路径记录", ""]
        # 按节点分组
        for n in nodes:
            node_edges = [e for e in self._edges if e.from_node == n.node_id]
            if not node_edges:
                continue
            lines.append(f"### [{n.node_id}] {n.title}")
            lines.append(f"**URL**: `{n.url}`\n")
            lines.append("| 操作元素 | 类型 | 区域 | 结果 | 摘要 | 跳转到 |")
            lines.append("|---------|------|------|------|------|--------|")
            for e in node_edges:
                # 找对应元素的区域
                elem_data = next(
                    (el for el in n.elements if el.get("description") == e.element_desc), {}
                )
                region = elem_data.get("region", "")
                new_url_short = (e.new_url[:40] + "...") if len(e.new_url) > 40 else e.new_url
                lines.append(
                    f"| {e.element_desc[:25]} | {e.elem_type} | {region} "
                    f"| {e.outcome} | {e.summary[:20]} | {new_url_short} |"
                )
            lines.append("")

        report_path = self.output_dir / "site_map.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print_success(f"📋 站点地图已生成: {report_path}")
