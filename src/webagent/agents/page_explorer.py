"""
PageExplorer — 区域感知自主探索智能体（Region-Aware Exhaustive Explorer）

核心架构: 区域优先 BFS + 跨页面 DFS + 持久化知识图谱

探索策略:
  1. Region Segmentation   — VLM 将当前页面识别为若干逻辑区域（导航/侧边栏/内容区等）
  2. Region-First BFS      — 逐区域枚举所有可交互元素，完整探索完一个区域再换下一个
  3. Cross-page DFS        — 操作引发页面跳转时，优先递归深入新页面，探完后回退继续上一层
  4. State Deduplication   — 基于 URL + DOM 摘要的状态哈希，避免重复访问同一状态
  5. Knowledge Graph Save  — 每次探索完一个节点即持久化，支持断点续探

最终产出:
  - exploration_graph.json  — 完整的系统状态图（节点=页面状态，边=交互操作）
  - exploration_report.md   — 可读的 Markdown 探索报告
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from webagent.agents.vision_engine import VisionEngine
from webagent.utils.logger import get_logger, print_agent, print_success, print_warning, print_error

logger = get_logger("webagent.agents.page_explorer")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PageRegion:
    """页面的一个逻辑区域（如导航栏、侧边栏、主内容区）"""
    region_id: str
    name: str           # 如 "顶部导航栏"、"左侧菜单"、"主内容区"
    region_type: str    # nav | sidebar | content | toolbar | footer | modal | other
    bbox: dict          # {"x1", "y1", "x2", "y2"} — 区域范围（CSS 像素）
    priority: int = 3   # 1-5，越高越优先探索


@dataclass
class InteractiveElement:
    """一个可交互元素"""
    element_id: str
    description: str
    element_type: str   # button | link | input | select | tab | menu | other
    coordinates: dict   # {"x": px, "y": px}（已完成坐标换算）
    region_id: str = ""
    selector_hint: str = ""
    explored: bool = False
    outcome: str = ""
    outcome_summary: str = ""


@dataclass
class ExplorationNode:
    """探索图中的一个节点（唯一页面状态）"""
    node_id: str
    url: str
    page_title: str
    state_hash: str
    depth: int
    screenshot_path: str = ""
    regions: list[dict] = field(default_factory=list)       # PageRegion 序列化
    elements: list[dict] = field(default_factory=list)      # InteractiveElement 序列化
    elements_explored: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ExplorationEdge:
    """探索图中的一条边（一次操作及其结果）"""
    edge_id: str
    from_node_id: str
    to_node_id: str
    element_description: str
    element_type: str
    action_type: str
    outcome: str        # page_changed | content_changed | dialog_appeared | same_page | error
    summary: str
    new_url: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class ExplorationGraph:
    """完整的探索知识图谱"""
    start_url: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    visited_hashes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VLM Prompts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SEGMENT_REGIONS_PROMPT = """你是一个专业的UI布局分析智能体。请分析这张页面截图，将页面划分为不同的逻辑功能区域。

## 划分原则
1. 按照视觉边界和功能差异划分（如导航栏、侧边栏、主内容区等）
2. 每个区域必须有明确的功能定位
3. 区域之间不应重叠
4. 忽略纯装饰性区域（纯背景色块、分隔线等）

## 区域类型
- nav: 顶部/顶栏导航区，包含菜单链接
- sidebar: 侧边栏，可能有子菜单或快速导航
- toolbar: 操作工具栏，包含新增/导出等功能按钮
- content: 主内容区，表格/卡片/表单等
- filter: 筛选/搜索区域
- footer: 底部页脚
- modal: 弹窗/对话框
- breadcrumb: 面包屑导航

返回 JSON（且仅返回 JSON）：
{{
    "page_title": "页面功能标题",
    "regions": [
        {{
            "region_id": "R1",
            "name": "区域的中文名称（如：顶部导航栏）",
            "type": "nav | sidebar | toolbar | content | filter | footer | other",
            "bbox": {{
                "x1": 左边界CSS像素,
                "y1": 上边界CSS像素,
                "x2": 右边界CSS像素,
                "y2": 下边界CSS像素
            }},
            "priority": 探索优先级1到5（5最高，nav/sidebar通常为5，content为4，footer为1）
        }}
    ]
}}

注意：坐标是相对于截图像素坐标，最多划分 8 个区域。
"""

ENUMERATE_IN_REGION_PROMPT = """你是一个专业的网页交互元素识别智能体。

## 当前任务
在这张**区域局部放大截图**中，识别出所有的可交互元素。

## 区域信息
区域名称: {region_name}
区域功能: {region_type}

## 识别规则
- 识别所有按钮、链接、菜单项、选择框、输入框等可点击元素
- 忽略灰色/禁用/不可点击的元素
- 忽略"删除"、"注销"等高危操作
- 图标 + 文字组合算一个元素，不要分开

返回 JSON（且仅返回 JSON）：
{{
    "elements": [
        {{
            "element_id": "E{region_id}_1",
            "description": "元素的文字内容或功能描述",
            "type": "button | link | input | select | tab | menu | checkbox | other",
            "coordinates": {{"x": 在本截图中的中心X坐标, "y": 在本截图中的中心Y坐标}},
            "selector_hint": "推测的CSS选择器（可为空）",
            "priority": 1到5的探索价值分（5最有价值）
        }}
    ]
}}
"""

ANALYZE_OUTCOME_PROMPT = """你是一个网页操作结果分析智能体。对比操作前后两张截图，分析这次操作的效果。

## 执行的操作
{action_description}

返回 JSON（且仅返回 JSON）：
{{
    "outcome": "page_changed | content_changed | dialog_appeared | same_page | error",
    "summary": "一句话描述本次操作的结果（中文，20字以内）",
    "is_reversible": true或false
}}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PageExplorer 主体
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PageExplorer:
    """
    区域感知自主探索智能体

    探索流程:
      对每个页面状态（节点）:
        1. 截图 → 识别区域划分
        2. 按区域优先级排序（nav/sidebar 先）
        3. 对每个区域: 放大截图 → 识别区域内所有元素
        4. 对每个元素: 执行操作 → 分析结果 → 若新状态则递归 → 回退
        5. 当前节点所有元素探索完毕 → 保存到图谱
    """

    def __init__(
        self,
        output_dir: str = "exploration_output",
        screenshots_dir: str = "screenshots/explorer",
    ):
        self.vision = VisionEngine(screenshots_dir=screenshots_dir)
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.graph = ExplorationGraph(start_url="")
        self._visited_hashes: set[str] = set()
        self._node_counter = 0
        self._edge_counter = 0

    # ── 公共接口 ──────────────────────────────────────────────────────

    async def explore(
        self,
        page: Page,
        max_depth: int = 3,
        max_elements_per_region: int = 8,
        max_total_nodes: int = 80,
        resume_graph_path: str | None = None,
    ) -> ExplorationGraph:
        """
        从当前 page 开始，进行区域感知全量自主探索

        Args:
            page: 已打开目标 URL 的 Playwright Page
            max_depth: 最大递归深度（每次跳转后还能深入多少层）
            max_elements_per_region: 每个区域最多探索多少个元素
            max_total_nodes: 全局页面状态数量上限（防止爆炸）
            resume_graph_path: 若指定，从已有图谱继续探索（断点续探）
        """
        start_url = page.url
        start_time = time.time()

        # 断点续探
        if resume_graph_path and Path(resume_graph_path).exists():
            self._load_graph(resume_graph_path)
            print_agent("page_explorer", f"📂 断点续探，已加载 {len(self.graph.nodes)} 个节点")
        else:
            self.graph = ExplorationGraph(start_url=start_url)
            self._visited_hashes = set()
            self._node_counter = 0
            self._edge_counter = 0

        print_agent("page_explorer", f"\n🌲 区域感知全量探索启动 → {start_url}")
        print_agent("page_explorer", f"参数: depth={max_depth} | elements/region={max_elements_per_region} | max_nodes={max_total_nodes}")

        await self._explore_node(
            page=page,
            depth=0,
            parent_node_id=None,
            parent_edge_element=None,
            max_depth=max_depth,
            max_elements_per_region=max_elements_per_region,
            max_total_nodes=max_total_nodes,
        )

        # 写入最终统计
        duration = round(time.time() - start_time, 1)
        self.graph.stats = {
            "duration_seconds": duration,
            "total_nodes": len(self.graph.nodes),
            "total_edges": len(self.graph.edges),
            "total_elements_explored": sum(n.get("elements_explored", 0) for n in self.graph.nodes),
        }
        self.graph.updated_at = time.time()

        # 保存图谱
        graph_path = str(self.output_dir / "exploration_graph.json")
        self._save_graph(graph_path)

        # 生成 Markdown 报告
        report_path = str(self.output_dir / "exploration_report.md")
        self._generate_markdown_report(report_path)

        self._print_summary()
        return self.graph

    # ── 核心探索逻辑 ─────────────────────────────────────────────────

    async def _explore_node(
        self,
        page: Page,
        depth: int,
        parent_node_id: str | None,
        parent_edge_element: InteractiveElement | None,
        max_depth: int,
        max_elements_per_region: int,
        max_total_nodes: int,
    ):
        """单个节点的完整区域探索"""

        # 检查全局上限
        if len(self.graph.nodes) >= max_total_nodes:
            return
        if depth > max_depth:
            return

        # 去重检查
        state_hash = await self._compute_state_hash(page)
        if state_hash in self._visited_hashes:
            print_agent("page_explorer", f"{'  '*depth}⟳ 已访问此状态，跳过")
            return
        self._visited_hashes.add(state_hash)
        self.graph.visited_hashes = list(self._visited_hashes)  # 同步到图谱

        # 建立节点
        self._node_counter += 1
        node_id = f"N{self._node_counter:04d}"
        try:
            page_title = await page.title()
        except Exception:
            page_title = page.url.split("/")[-1] or "untitled"

        print_agent(
            "page_explorer",
            f"\n{'  '*depth}{'━'*50}\n"
            f"{'  '*depth}🔍 [{node_id}] 探索节点 depth={depth}: {page_title[:40]} | {page.url[:50]}"
        )

        # ── Step 1: 全页面截图（带 SOM 标注）──
        screenshot_path = await self.vision._screenshot(page, f"node_{node_id}", draw_som=True)

        # ── Step 2: VLM 区域划分 ──
        regions = await self._segment_regions(page, screenshot_path)
        if not regions:
            # 无法划分区域，当作单一区域处理
            regions = [PageRegion(
                region_id="R0",
                name="整个页面",
                region_type="content",
                bbox={"x1": 0, "y1": 0, "x2": 1280, "y2": 800},
                priority=3,
            )]

        print_agent("page_explorer", f"{'  '*depth}  📐 识别到 {len(regions)} 个区域: {[r.name for r in regions]}")

        # ── Step 3: 按优先级探索每个区域 ──
        regions_sorted = sorted(regions, key=lambda r: -r.priority)

        node = ExplorationNode(
            node_id=node_id,
            url=page.url,
            page_title=page_title,
            state_hash=state_hash,
            depth=depth,
            screenshot_path=screenshot_path,
            regions=[asdict(r) for r in regions],
        )

        all_elements: list[InteractiveElement] = []

        for region in regions_sorted:
            print_agent(
                "page_explorer",
                f"{'  '*depth}  🗺️  探索区域: [{region.region_id}] {region.name} (优先级={region.priority})"
            )

            # 获取区域内元素
            region_elements = await self._enumerate_region_elements(page, screenshot_path, region)
            region_elements = region_elements[:max_elements_per_region]
            all_elements.extend(region_elements)

            print_agent(
                "page_explorer",
                f"{'  '*depth}    发现 {len(region_elements)} 个元素: "
                f"{[e.description[:12] for e in region_elements[:5]]}"
            )

            # 逐个探索该区域内的元素
            for i, element in enumerate(region_elements):
                if len(self.graph.nodes) >= max_total_nodes:
                    break

                indent = "  " * depth
                print_agent(
                    "page_explorer",
                    f"{indent}    → [{i+1}/{len(region_elements)}] "
                    f"[{element.element_type}] {element.description[:45]}"
                )

                # 保存快照
                snapshot = await self._save_snapshot(page)
                url_before = page.url
                ss_before = await self.vision._screenshot(page, f"action_before_{node_id}_{region.region_id}_{i}")

                # 执行操作（带三阶坐标精修）
                success = await self._execute_element_action(page, element)

                if not success:
                    print_warning(f"{indent}      ❌ 操作失败")
                    element.outcome = "error"
                    element.explored = True
                    await self._restore_snapshot(page, snapshot)
                    continue

                await VisionEngine._wait_stable(page)

                url_after = page.url
                state_hash_after = await self._compute_state_hash(page)
                is_new_state = state_hash_after not in self._visited_hashes

                # 分析结果
                ss_after = await self.vision._screenshot(page, f"action_after_{node_id}_{region.region_id}_{i}")
                outcome_data = await self._analyze_outcome(
                    ss_before, ss_after,
                    f"{element.element_type}: {element.description}"
                )
                outcome = outcome_data.get("outcome", "same_page")
                summary = outcome_data.get("summary", "")

                element.outcome = outcome
                element.outcome_summary = summary
                element.explored = True
                node.elements_explored += 1

                print_success(f"{indent}      ✅ {outcome}: {summary[:50]}")

                # 建立边记录
                self._edge_counter += 1
                edge = ExplorationEdge(
                    edge_id=f"E{self._edge_counter:05d}",
                    from_node_id=node_id,
                    to_node_id="",
                    element_description=element.description,
                    element_type=element.element_type,
                    action_type="click",
                    outcome=outcome,
                    summary=summary,
                    new_url=url_after if url_after != url_before else "",
                )

                # 若产生新状态 → 递归深入探索
                if is_new_state and outcome in ("page_changed", "content_changed", "dialog_appeared"):
                    print_agent("page_explorer", f"{indent}      🌿 发现新状态，递归探索 (depth={depth+1})...")

                    await self._explore_node(
                        page=page,
                        depth=depth + 1,
                        parent_node_id=node_id,
                        parent_edge_element=element,
                        max_depth=max_depth,
                        max_elements_per_region=max_elements_per_region,
                        max_total_nodes=max_total_nodes,
                    )
                    # 记录到边
                    edge.to_node_id = f"N{self._node_counter:04d}"

                self.graph.edges.append(asdict(edge))

                # 回退到操作前快照，探索下一个元素
                await self._restore_snapshot(page, snapshot)

            # 每探索完一个区域，立即持久化（防止中途崩溃丢失数据）
            self._save_graph(str(self.output_dir / "exploration_graph.json"))

        # 节点记录
        node.elements = [asdict(e) for e in all_elements]
        self.graph.nodes.append(asdict(node))
        self.graph.updated_at = time.time()

    # ── 区域划分 ────────────────────────────────────────────────────

    async def _segment_regions(self, page: Page, screenshot_path: str) -> list[PageRegion]:
        """VLM 识别页面区域并将坐标换算为 CSS 逻辑像素"""
        try:
            response = await self.vision._call_vision_llm(SEGMENT_REGIONS_PROMPT, [screenshot_path])
            data = self.vision._extract_json(response)
            if not data or "regions" not in data:
                return []

            # 获取图片尺寸 & 视口用于坐标换算
            from PIL import Image
            with Image.open(screenshot_path) as img:
                img_w, img_h = img.size
            vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            vw, vh = int(vp["w"]), int(vp["h"])
            sx, sy = vw / img_w, vh / img_h

            result = []
            for r in data["regions"]:
                bbox = r.get("bbox", {})
                # 换算 bbox 坐标
                css_bbox = {
                    "x1": max(0, int(bbox.get("x1", 0) * sx)),
                    "y1": max(0, int(bbox.get("y1", 0) * sy)),
                    "x2": min(vw, int(bbox.get("x2", img_w) * sx)),
                    "y2": min(vh, int(bbox.get("y2", img_h) * sy)),
                }
                result.append(PageRegion(
                    region_id=str(r.get("region_id", f"R{len(result)+1}")),
                    name=r.get("name", "未命名区域"),
                    region_type=r.get("type", "other"),
                    bbox=css_bbox,
                    priority=int(r.get("priority", 3)),
                ))
            return result
        except Exception as e:
            logger.warning(f"区域划分失败: {e}")
            return []

    # ── 区域内元素枚举 ────────────────────────────────────────────────

    async def _enumerate_region_elements(
        self, page: Page, full_screenshot_path: str, region: PageRegion
    ) -> list[InteractiveElement]:
        """裁剪区域截图 → VLM 识别区域内元素 → 坐标换算回全页面坐标"""
        try:
            from PIL import Image

            # 读取全页面截图尺寸，计算物理像素边界
            with Image.open(full_screenshot_path) as img:
                img_w, img_h = img.size
            vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            vw, vh = int(vp["w"]), int(vp["h"])
            sx, sy = img_w / vw, img_h / vh  # CSS → 物理像素比

            # 将 CSS 坐标的 bbox 换算回物理像素，用于裁剪
            b = region.bbox
            crop_x1 = max(0, int(b["x1"] * sx))
            crop_y1 = max(0, int(b["y1"] * sy))
            crop_x2 = min(img_w, int(b["x2"] * sx))
            crop_y2 = min(img_h, int(b["y2"] * sy))

            if crop_x2 - crop_x1 < 30 or crop_y2 - crop_y1 < 30:
                return []  # 区域太小

            # 裁剪并放大到 768×768（letterbox）
            zoom_size = 768
            with Image.open(full_screenshot_path) as img:
                cropped = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))

            cw, ch = cropped.size
            ratio = min(zoom_size / cw, zoom_size / ch)
            nw, nh = int(cw * ratio), int(ch * ratio)
            resized = cropped.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGB", (zoom_size, zoom_size), (30, 30, 30))
            px, py = (zoom_size - nw) // 2, (zoom_size - nh) // 2
            canvas.paste(resized, (px, py))

            ts = int(time.time() * 1000)
            region_path = str(self.screenshots_dir / f"region_{region.region_id}_{ts}.jpg")
            canvas.save(region_path, "JPEG", quality=90)

            # VLM 识别区域内元素
            prompt = ENUMERATE_IN_REGION_PROMPT.format(
                region_name=region.name,
                region_type=region.region_type,
                region_id=region.region_id,
            )
            response = await self.vision._call_vision_llm(prompt, [region_path])
            data = self.vision._extract_json(response)
            if not data or "elements" not in data:
                return []

            raw_elements = sorted(data["elements"], key=lambda e: -e.get("priority", 3))
            result = []

            for raw in raw_elements:
                rc = raw.get("coordinates", {})
                zoom_x, zoom_y = int(rc.get("x", 0)), int(rc.get("y", 0))
                if zoom_x <= 0 and zoom_y <= 0:
                    continue

                # 逆向换算：放大图坐标 → 裁剪图物理坐标 → 全图物理坐标 → CSS 坐标
                local_x = (zoom_x - px) / ratio   # 裁剪区域内的物理像素坐标
                local_y = (zoom_y - py) / ratio
                global_px_x = crop_x1 + local_x    # 全图物理坐标
                global_px_y = crop_y1 + local_y
                css_x = int(global_px_x / sx)       # CSS 逻辑像素
                css_y = int(global_px_y / sy)

                # Clamp 到视口
                css_x = max(0, min(vw, css_x))
                css_y = max(0, min(vh, css_y))

                result.append(InteractiveElement(
                    element_id=str(raw.get("element_id", f"{region.region_id}_auto_{len(result)+1}")),
                    description=raw.get("description", ""),
                    element_type=raw.get("type", "other"),
                    coordinates={"x": css_x, "y": css_y},
                    region_id=region.region_id,
                    selector_hint=raw.get("selector_hint", ""),
                ))
            return result

        except Exception as e:
            logger.warning(f"区域元素枚举失败 [{region.name}]: {e}")
            return []

    # ── 操作执行 ────────────────────────────────────────────────────

    async def _execute_element_action(self, page: Page, element: InteractiveElement) -> bool:
        """带三阶坐标精修的操作执行"""
        from webagent.agents.vision_engine import VisionAction
        try:
            x, y = element.coordinates["x"], element.coordinates["y"]

            # 二阶精修（局部放大再定位）
            rx, ry, zoom_conf = await self.vision._zoom_refine_coords(
                page, x, y, element.description, zoom_radius=180
            )
            if zoom_conf >= 0.45:
                x, y = rx, ry

            # DOM 精修吸附
            fx, fy, sel, _ = await self.vision._refine_coordinates(page, x, y, element.description)

            action = VisionAction(
                action_type="click",
                target_description=element.description,
                coordinates={"x": fx, "y": fy},
                element_id=element.element_id,
                selector_hint=element.selector_hint or sel,
            )
            return await self.vision.execute_vision_action(page, action)
        except Exception as e:
            logger.debug(f"操作异常: {e}")
            return False

    # ── 结果分析 ────────────────────────────────────────────────────

    async def _analyze_outcome(self, ss_before: str, ss_after: str, action_desc: str) -> dict:
        """VLM 对比前后截图，分析操作结果"""
        try:
            prompt = ANALYZE_OUTCOME_PROMPT.format(action_description=action_desc)
            response = await self.vision._call_vision_llm(prompt, [ss_before, ss_after])
            return self.vision._extract_json(response) or {"outcome": "same_page", "summary": ""}
        except Exception as e:
            logger.debug(f"结果分析失败: {e}")
            return {"outcome": "same_page", "summary": ""}

    # ── 快照与状态管理 ───────────────────────────────────────────────

    async def _save_snapshot(self, page: Page) -> dict:
        return {
            "url": page.url,
            "scroll_x": await page.evaluate("window.scrollX"),
            "scroll_y": await page.evaluate("window.scrollY"),
        }

    async def _restore_snapshot(self, page: Page, snapshot: dict):
        target_url = snapshot["url"]
        if page.url != target_url:
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
                await VisionEngine._wait_stable(page)
            except Exception as e:
                logger.warning(f"回退失败: {e}")
                return
        try:
            await page.evaluate(f"window.scrollTo({snapshot['scroll_x']}, {snapshot['scroll_y']})")
        except Exception:
            pass
        await asyncio.sleep(0.15)

    async def _compute_state_hash(self, page: Page) -> str:
        try:
            url_base = page.url.split("?")[0]
            dom_text = await page.evaluate("""() => {
                const body = document.body;
                return body ? body.innerText.substring(0, 600).replace(/\\s+/g, ' ').trim() : '';
            }""")
            return hashlib.md5(f"{url_base}||{dom_text}".encode()).hexdigest()[:16]
        except Exception:
            return hashlib.md5(page.url.encode()).hexdigest()[:16]

    # ── 图谱持久化 ────────────────────────────────────────────────────

    def _save_graph(self, path: str):
        """保存探索图谱 JSON"""
        data = {
            "start_url": self.graph.start_url,
            "created_at": self.graph.created_at,
            "updated_at": time.time(),
            "stats": self.graph.stats,
            "nodes": self.graph.nodes,
            "edges": self.graph.edges,
            "visited_hashes": list(self._visited_hashes),
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_graph(self, path: str):
        """加载已有图谱，用于断点续探"""
        data = json.loads(Path(path).read_text())
        self.graph = ExplorationGraph(
            start_url=data.get("start_url", ""),
            nodes=data.get("nodes", []),
            edges=data.get("edges", []),
            visited_hashes=data.get("visited_hashes", []),
            stats=data.get("stats", {}),
        )
        self._visited_hashes = set(self.graph.visited_hashes)
        self._node_counter = len(self.graph.nodes)
        self._edge_counter = len(self.graph.edges)

    def _generate_markdown_report(self, path: str):
        """生成 Markdown 可读探索报告"""
        g = self.graph
        stats = g.stats
        lines = [
            f"# 🌲 Web 系统探索报告",
            f"",
            f"**起始URL**: {g.start_url}",
            f"**探索耗时**: {stats.get('duration_seconds', '?')}s",
            f"",
            f"## 📊 统计汇总",
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 探索页面状态数 | {stats.get('total_nodes', 0)} |",
            f"| 操作路径数（边） | {stats.get('total_edges', 0)} |",
            f"| 已探索元素总数 | {stats.get('total_elements_explored', 0)} |",
            f"",
            f"## 📄 页面节点详情",
            f"",
        ]
        for node in g.nodes:
            lines.append(f"### [{node['node_id']}] {node['page_title']}")
            lines.append(f"- **URL**: `{node['url']}`")
            lines.append(f"- **深度**: {node['depth']}")
            lines.append(f"- **识别区域**: {len(node.get('regions', []))}")
            lines.append(f"- **已探索元素**: {node.get('elements_explored', 0)}")
            if node.get("regions"):
                region_names = [r["name"] for r in node["regions"]]
                lines.append(f"- **区域列表**: {', '.join(region_names)}")
            lines.append("")

        lines.append("## 🔀 操作路径（边）")
        lines.append("")
        lines.append("| 边ID | 从节点 | 操作元素 | 结果 | 摘要 |")
        lines.append("|------|--------|---------|------|------|")
        for edge in g.edges[:100]:  # 最多显示100条
            lines.append(
                f"| {edge.get('edge_id','')} | {edge.get('from_node_id','')} | "
                f"{edge.get('element_description','')[:25]} | "
                f"{edge.get('outcome','')} | {edge.get('summary','')[:30]} |"
            )

        Path(path).write_text("\n".join(lines), encoding="utf-8")
        print_success(f"📋 Markdown 报告已生成: {path}")

    def _print_summary(self):
        s = self.graph.stats
        print_success(
            f"\n{'='*60}\n"
            f"🎉 全量探索完成！\n"
            f"  ⏱️  耗时:       {s.get('duration_seconds','?')}s\n"
            f"  📄 页面节点:   {s.get('total_nodes', 0)}\n"
            f"  🔀 操作路径:   {s.get('total_edges', 0)}\n"
            f"  🎯 已探索元素: {s.get('total_elements_explored', 0)}\n"
            f"{'='*60}\n"
            f"📊 结果保存于: {self.output_dir}/"
        )
