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

        # BFS 队列: (url, depth, from_node_id)
        self._queue: deque[tuple[str, int, str]] = deque()
        # 已加入队列的 URL（去掉 query 的标准化路径，防重复入队）
        self._queued_urls: set[str] = set()

        # 域名限制（探索时不跨域）
        self._allowed_origin: str = ""

    # ── 公共接口 ──────────────────────────────────────────

    async def explore(
        self,
        page: Page,
        max_depth: int = 3,
        max_nodes: int = 80,
        max_elements_per_page: int = 20,
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
            self._queue.append((start_url, 0, ""))
            self._queued_urls.add(self._normalize_url(start_url))

        print_agent("site_explorer", f"\n🌐 Web 系统全量探索启动")
        print_agent("site_explorer", f"   起始地址: {start_url}")
        print_agent("site_explorer", f"   同域限制: {self._allowed_origin}")
        print_agent("site_explorer", f"   参数: max_depth={max_depth} | max_nodes={max_nodes} | elements/page={max_elements_per_page}")
        print_agent("site_explorer", "─" * 60)

        # ── BFS 主循环 ──
        while self._queue and len(self._nodes) < max_nodes:
            target_url, depth, from_node_id = self._queue.popleft()

            if depth > max_depth:
                continue

            # 导航到目标页面
            try:
                if page.url != target_url:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                await VisionEngine._wait_stable(page)
            except Exception as e:
                logger.warning(f"无法导航到 {target_url}: {e}")
                continue

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
                elements_found=len(elements),
            )

            # 按区域 + 优先级排序（nav/sidebar 最先探索）
            region_order = {"nav": 0, "sidebar": 1, "toolbar": 2, "breadcrumb": 3, "content": 4, "footer": 5, "other": 6}
            elements_sorted = sorted(
                elements[:max_elements_per_page],
                key=lambda e: (region_order.get(e.region_name, 6), -e.priority)
            )

            # ── 逐元素探索 ──
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
                if new_hash not in self._visited and outcome in ("page_changed", "content_changed", "dialog_appeared"):
                    new_url = page.url
                    if self._is_same_origin(new_url):
                        norm = self._normalize_url(new_url)
                        if norm not in self._queued_urls:
                            self._queued_urls.add(norm)
                            self._queue.append((new_url, depth + 1, node_id))
                            print_agent("site_explorer", f"    ➕ 新页面入队: {new_url[:60]} (depth={depth+1})")

                edge.to_node = new_url if url_after != url_before else ""
                self._edges.append(edge)

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
        """VLM 一次性从截图中提取所有可交互元素，并完成坐标换算"""
        try:
            response = await self.vision._call_vision_llm(DISCOVER_ELEMENTS_PROMPT, [screenshot_path])
            data = self.vision._extract_json(response)
            if not data or "elements" not in data:
                return []

            # 读取截图尺寸与视口大小，用于坐标换算
            from PIL import Image
            with Image.open(screenshot_path) as img:
                img_w, img_h = img.size
            vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            vw, vh = int(vp["w"]), int(vp["h"])

            result = []
            for raw in data["elements"]:
                img_x = int(raw.get("x", 0))
                img_y = int(raw.get("y", 0))
                if img_x <= 0 and img_y <= 0:
                    continue

                # 坐标换算：截图像素 → CSS 逻辑像素（含全局偏移修复）
                css_x, css_y = await self.vision._scale_llm_coords(page, img_x, img_y, screenshot_path)
                css_x = max(0, min(vw, css_x))
                css_y = max(0, min(vh, css_y))

                result.append(DiscoveredElement(
                    elem_id=str(raw.get("id", f"auto_{len(result)+1}")),
                    description=raw.get("description", ""),
                    elem_type=raw.get("type", "other"),
                    css_x=css_x,
                    css_y=css_y,
                    selector_hint=raw.get("selector_hint", ""),
                    region_name=raw.get("region", "other"),
                    priority=int(raw.get("priority", 3)),
                ))
            return result

        except Exception as e:
            logger.warning(f"元素发现失败: {e}")
            return []

    # ── 点击执行 ──────────────────────────────────────────

    async def _click_element(self, page: Page, elem: DiscoveredElement, screenshot_path: str) -> bool:
        """带三阶坐标精修的元素点击"""
        from webagent.agents.vision_engine import VisionAction
        try:
            x, y = elem.css_x, elem.css_y

            # 二阶精修（局部放大再定位）
            rx, ry, zoom_conf = await self.vision._zoom_refine_coords(
                page, x, y, elem.description, zoom_radius=160
            )
            if zoom_conf >= 0.45:
                x, y = rx, ry

            # DOM 吸附
            fx, fy, sel, _ = await self.vision._refine_coordinates(page, x, y, elem.description)

            action = VisionAction(
                action_type="click",
                target_description=elem.description,
                coordinates={"x": fx, "y": fy},
                element_id=elem.elem_id,
                selector_hint=elem.selector_hint or sel,
            )
            return await self.vision.execute_vision_action(page, action)
        except Exception as e:
            logger.debug(f"点击异常: {e}")
            return False

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
        return {
            "url": page.url,
            "sx": await page.evaluate("window.scrollX"),
            "sy": await page.evaluate("window.scrollY"),
        }

    async def _restore(self, page: Page, snapshot: dict):
        target = snapshot["url"]
        if page.url != target:
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=12000)
                await VisionEngine._wait_stable(page)
            except Exception as e:
                logger.warning(f"回退失败: {e}")
                return
        try:
            await page.evaluate(f"window.scrollTo({snapshot['sx']}, {snapshot['sy']})")
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
        """去掉 query string / fragment，只保留 scheme+host+path"""
        try:
            p = urlparse(url)
            return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
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
        for n in data.get("nodes", []):
            node = SiteNode(
                node_id=n["node_id"], url=n["url"], url_path=n.get("url_path", ""),
                title=n["title"], depth=n["depth"], state_hash=n["state_hash"],
                screenshot=n.get("screenshot", ""), elements=n.get("elements", []),
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
                self._queue.append((url, 0, ""))

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
