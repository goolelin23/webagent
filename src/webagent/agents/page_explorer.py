"""
PageExplorer — 页面全量自主探索智能体

核心理念：穷举树形遍历（Exhaustive DFS Tree Traversal）
  给定一个起始页面，让 agent 自动：
  1. 从页面截图中识别出所有可交互元素（按钮、链接、菜单项、下拉框等）
  2. 依次点击每一个未探索的元素，观察其产生的结果
  3. 如果页面发生跳转/变化，继续在新状态中递归探索
  4. 探索完成后回退到操作前快照，再尝试下一个元素
  5. 直到所有可交互元素都被探索过一次

这与 ActiveLearner 的"目标导向执行"截然不同：
  - ActiveLearner: 有目标 → 选最优路径 → 执行
  - PageExplorer:  无目标 → 穷举所有分支 → DFS 递归 → 建图
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
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
class InteractiveElement:
    """页面上识别到的一个可交互元素"""
    element_id: str          # SOM 数字 ID（截图中标注的数字）
    description: str         # 元素文本/描述
    element_type: str        # button | link | input | select | menu | tab | other
    coordinates: dict        # {"x": int, "y": int}
    selector_hint: str = ""  # CSS 选择器提示（如有）
    explored: bool = False   # 是否已被探索


@dataclass
class ExplorationNode:
    """探索树中的一个节点"""
    node_id: str
    url: str
    page_title: str
    screenshot_path: str
    elements: list[InteractiveElement] = field(default_factory=list)
    children: list["ExplorationEdge"] = field(default_factory=list)
    depth: int = 0


@dataclass
class ExplorationEdge:
    """探索树中一条边（一次操作 → 跳到的新状态）"""
    from_node_id: str
    element: InteractiveElement
    action_type: str           # click | fill | select
    action_value: str = ""
    to_node_id: str = ""
    outcome: str = ""          # page_changed | dialog | same_page | error
    page_changed: bool = False
    new_url: str = ""
    summary: str = ""          # 操作结果摘要


@dataclass
class ExplorationReport:
    """完整探索报告"""
    start_url: str
    total_nodes: int = 0
    total_edges: int = 0
    total_elements_found: int = 0
    total_elements_explored: int = 0
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    blocked_elements: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 视觉元素识别 Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ENUMERATE_ELEMENTS_PROMPT = """你是一个专业的网页交互元素识别智能体。请仔细分析这张截图，列出所有**可点击的交互元素**。

## 要求
识别所有用户可以与之交互的元素，包括但不限于：
- 按钮（Button）、提交按钮
- 超链接（Link/Anchor）（包括文字链接和图标链接）
- 导航菜单项、标签页（Tabs）
- 下拉框/选择框（Select）
- 复选框、单选按钮
- 输入框（Input）
- 图标按钮、工具栏按钮

## 元素来源
截图中标注了带边框的红色数字标签，这是已识别的 SOM 标签编号（e.g. 1, 2, 3）。
请优先使用这些已标注的元素，同时也可以识别截图中未被标注但明显可交互的元素。

## 过滤规则
- 忽略纯装饰性元素（图片、背景、分隔线）
- 忽略已被灰色置灰、禁用状态的元素
- 忽略"注销"、"删除账号"等高危操作元素（为安全起见）

返回 JSON（且仅返回 JSON）：
{{
    "page_title": "当前页面的标题或主要功能描述",
    "elements": [
        {{
            "element_id": "截图中的 SOM 数字标签（若无标签则用 auto_1, auto_2...）",
            "description": "元素的文字内容或功能描述（如：'用户管理'菜单、'提交'按钮）",
            "type": "button | link | input | select | tab | menu | checkbox | other",
            "coordinates": {{"x": 元素中心X坐标, "y": 元素中心Y坐标}},
            "selector_hint": "如果能推测出CSS选择器，填写（可为空）",
            "priority": 1到5的数字，5为最值得探索，1为最低优先级
        }}
    ]
}}

重要：坐标是基于本截图图像的像素坐标。最多返回 20 个最有探索价值的元素。
"""

SUMMARIZE_OUTCOME_PROMPT = """你是一个网页操作结果分析智能体。请对比操作前后两张截图，分析操作结果。

## 执行的操作
{action_description}

请分析这次操作产生了什么效果，返回 JSON：
{{
    "outcome": "page_changed（跳转到了新页面） | content_changed（内容变化但URL未变） | dialog_appeared（出现弹窗/模态框） | same_page（几乎没有变化） | error（出现了错误提示）",
    "summary": "一句话描述本次操作的结果",
    "new_elements_visible": true或false,
    "is_reversible": true（操作前后一致，可回退）或false（有副作用，比如提交了表单）
}}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PageExplorer 主体
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PageExplorer:
    """
    页面全量自主探索智能体

    使用深度优先遍历（DFS），对页面上的所有可交互元素
    依次尝试操作，记录每一条路径的结果，直到所有分支探索完毕。
    """

    def __init__(self, screenshots_dir: str = "screenshots/explorer"):
        self.vision = VisionEngine(screenshots_dir=screenshots_dir)
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self.report = ExplorationReport(start_url="")
        self._visited_state_hashes: set[str] = set()  # 去重：避免反复探索同一状态
        self._node_counter = 0

    # ── 公共接口 ──────────────────────────────────────────────────────

    async def explore(
        self,
        page: Page,
        max_depth: int = 3,
        max_elements_per_page: int = 15,
        max_total_nodes: int = 100,
    ) -> ExplorationReport:
        """
        主入口：从当前 page 开始全量自主探索

        Args:
            page: Playwright Page 对象（已打开目标 URL）
            max_depth: 最大递归深度（每次操作后产生的新状态再往下探多深）
            max_elements_per_page: 每个页面状态最多尝试多少个元素
            max_total_nodes: 全局最多探索多少个页面状态节点（防止爆炸）

        Returns:
            ExplorationReport 完整探索报告
        """
        start_url = page.url
        self.report = ExplorationReport(start_url=start_url)
        self._visited_state_hashes = set()
        self._node_counter = 0
        start_time = time.time()

        print_agent("page_explorer", f"🚀 启动全量自主探索: {start_url}")
        print_agent("page_explorer", f"📋 参数: max_depth={max_depth}, max_elements_per_page={max_elements_per_page}, max_nodes={max_total_nodes}")

        await self._dfs_explore(
            page=page,
            depth=0,
            max_depth=max_depth,
            max_elements_per_page=max_elements_per_page,
            max_total_nodes=max_total_nodes,
            ancestor_snapshot=None,
        )

        self.report.duration_seconds = round(time.time() - start_time, 1)
        self._print_final_report()
        return self.report

    # ── 核心 DFS 递归 ────────────────────────────────────────────────

    async def _dfs_explore(
        self,
        page: Page,
        depth: int,
        max_depth: int,
        max_elements_per_page: int,
        max_total_nodes: int,
        ancestor_snapshot: dict | None,
    ):
        """
        DFS 主递归体

        流程:
          1. 计算当前页面状态签名（去重）
          2. 截图 + 调用 VLM 识别本页所有交互元素
          3. 依次对每个未探索元素执行操作
          4. 记录操作结果（快照比较）
          5. 如果页面状态改变 → 递归 _dfs_explore
          6. 回退到操作前快照，尝试下一个元素
        """
        if self.report.total_nodes >= max_total_nodes:
            logger.info(f"达到节点上限({max_total_nodes})，停止探索")
            return

        if depth > max_depth:
            logger.debug(f"达到最大深度({max_depth})，回退")
            return

        # ── Step 1: 去重状态检查 ──
        state_hash = await self._compute_state_hash(page)
        if state_hash in self._visited_state_hashes:
            print_agent("page_explorer", f"{'  '*depth}⟳ 状态已访问过，跳过: {page.url[:60]}")
            return
        self._visited_state_hashes.add(state_hash)

        # ── Step 2: 为当前状态创建节点 ──
        self._node_counter += 1
        node_id = f"N{self._node_counter:04d}"
        self.report.total_nodes += 1

        print_agent(
            "page_explorer",
            f"\n{'  '*depth}{'─'*40}\n"
            f"{'  '*depth}🔍 探索节点 [{node_id}] 深度={depth}: {page.url[:70]}"
        )

        # ── Step 3: 截图（带 SOM 标注）──
        screenshot_path = await self.vision._screenshot(page, f"explorer_{node_id}", draw_som=True)

        # ── Step 4: VLM 识别所有可交互元素 ──
        elements = await self._enumerate_elements(page, screenshot_path)
        self.report.total_elements_found += len(elements)

        if not elements:
            print_agent("page_explorer", f"{'  '*depth}⚠️ 未发现可交互元素，结束本节点")
            return

        print_agent("page_explorer", f"{'  '*depth}📋 发现 {len(elements)} 个可交互元素")

        # 限制每个页面探索的元素数量
        elements_to_explore = elements[:max_elements_per_page]

        # ── Step 5: DFS —— 依次探索每个元素 ──
        for idx, element in enumerate(elements_to_explore):
            if self.report.total_nodes >= max_total_nodes:
                break

            indent = "  " * depth
            print_agent(
                "page_explorer",
                f"{indent}  → [{idx+1}/{len(elements_to_explore)}] "
                f"[{element.element_type}] {element.description[:50]}"
            )

            # 保存当前快照（用于操作后回退）
            snapshot_before = await self._save_snapshot(page)
            url_before = page.url
            screenshot_before = await self.vision._screenshot(page, f"before_{node_id}_{idx}")

            # 执行操作
            success, outcome_summary = await self._attempt_action(page, element)

            if not success:
                print_warning(f"{indent}    ❌ 操作失败，跳过")
                self.report.blocked_elements.append({
                    "node_id": node_id,
                    "element": element.description,
                    "reason": "execution_failed",
                })
                await self._restore_snapshot(page, snapshot_before)
                continue

            # 等待页面稳定
            await VisionEngine._wait_stable(page)

            # 截图对比 + 结果分析
            url_after = page.url
            screenshot_after = await self.vision._screenshot(page, f"after_{node_id}_{idx}")
            page_changed = (url_after != url_before)
            new_state_hash = await self._compute_state_hash(page)
            state_is_new = new_state_hash not in self._visited_state_hashes

            # 调用 VLM 总结操作结果
            outcome_data = await self._analyze_outcome(
                screenshot_before, screenshot_after,
                f"{element.element_type} {element.description}"
            )
            outcome = outcome_data.get("outcome", "same_page")
            summary = outcome_data.get("summary", "")

            edge = ExplorationEdge(
                from_node_id=node_id,
                element=element,
                action_type="click",
                to_node_id="",
                outcome=outcome,
                page_changed=page_changed,
                new_url=url_after if page_changed else "",
                summary=summary,
            )

            element.explored = True
            self.report.total_elements_explored += 1
            self.report.total_edges += 1

            print_success(f"{indent}    ✅ {outcome}: {summary[:60]}")

            # ── 递归：如果状态发生变化且是新状态，深入探索 ──
            if state_is_new and outcome in ("page_changed", "content_changed", "dialog_appeared"):
                print_agent("page_explorer", f"{indent}    🌿 发现新状态，递归探索 (depth={depth+1})...")
                child_snapshot = await self._save_snapshot(page)
                await self._dfs_explore(
                    page=page,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_elements_per_page=max_elements_per_page,
                    max_total_nodes=max_total_nodes,
                    ancestor_snapshot=child_snapshot,
                )
                edge.to_node_id = f"N{self._node_counter:04d}"

            self.report.edges.append({
                "from": edge.from_node_id,
                "element": edge.element.description,
                "outcome": edge.outcome,
                "new_url": edge.new_url,
                "summary": edge.summary,
            })

            # ── 回退到本操作前的状态，继续探索下一个元素 ──
            await self._restore_snapshot(page, snapshot_before)

        self.report.nodes.append({
            "id": node_id,
            "url": page.url,
            "depth": depth,
            "elements_found": len(elements),
            "elements_explored": sum(1 for e in elements_to_explore if e.explored),
        })

    # ── 元素识别 ────────────────────────────────────────────────────

    async def _enumerate_elements(self, page: Page, screenshot_path: str) -> list[InteractiveElement]:
        """调用 VLM 从截图中识别所有可交互元素"""
        try:
            response = await self.vision._call_vision_llm(
                ENUMERATE_ELEMENTS_PROMPT, [screenshot_path]
            )
            data = self.vision._extract_json(response)
            if not data or "elements" not in data:
                return []

            raw_elements = data.get("elements", [])

            # 按优先级排序（数字越大越优先）
            raw_elements.sort(key=lambda e: -e.get("priority", 3))

            result = []
            for raw in raw_elements:
                coords = raw.get("coordinates", {})
                llm_x = int(coords.get("x", 0))
                llm_y = int(coords.get("y", 0))

                # 坐标转换：LLM 图像像素 → CSS 逻辑像素（使用三阶管线第一阶）
                if llm_x > 0 and llm_y > 0:
                    css_x, css_y = await self.vision._scale_llm_coords(page, llm_x, llm_y, screenshot_path)
                else:
                    css_x, css_y = llm_x, llm_y

                result.append(InteractiveElement(
                    element_id=str(raw.get("element_id", "?")),
                    description=raw.get("description", ""),
                    element_type=raw.get("type", "other"),
                    coordinates={"x": css_x, "y": css_y},
                    selector_hint=raw.get("selector_hint", ""),
                ))
            return result

        except Exception as e:
            logger.warning(f"元素识别失败: {e}")
            return []

    # ── 操作执行 ────────────────────────────────────────────────────

    async def _attempt_action(self, page: Page, element: InteractiveElement) -> tuple[bool, str]:
        """尝试对元素执行操作"""
        from webagent.agents.vision_engine import VisionAction

        try:
            # 先尝试二阶精修坐标
            x, y = element.coordinates["x"], element.coordinates["y"]
            rx, ry, zoom_conf = await self.vision._zoom_refine_coords(
                page, x, y, element.description, zoom_radius=200
            )
            if zoom_conf >= 0.5:
                x, y = rx, ry

            # DOM 精修吸附
            fx, fy, _sel, _method = await self.vision._refine_coordinates(page, x, y, element.description)

            # 构建 VisionAction 执行
            action = VisionAction(
                action_type="click",
                target_description=element.description,
                coordinates={"x": fx, "y": fy},
                element_id=element.element_id,
                selector_hint=element.selector_hint or _sel,
            )
            success = await self.vision.execute_vision_action(page, action)
            return success, ""
        except Exception as e:
            logger.debug(f"操作异常: {e}")
            return False, str(e)

    # ── 结果分析 ────────────────────────────────────────────────────

    async def _analyze_outcome(
        self,
        screenshot_before: str,
        screenshot_after: str,
        action_description: str,
    ) -> dict:
        """调用 VLM 对比操作前后截图，分析操作结果"""
        try:
            prompt = SUMMARIZE_OUTCOME_PROMPT.format(action_description=action_description)
            response = await self.vision._call_vision_llm(prompt, [screenshot_before, screenshot_after])
            data = self.vision._extract_json(response)
            return data or {"outcome": "same_page", "summary": "无法分析结果"}
        except Exception as e:
            logger.debug(f"结果分析失败: {e}")
            return {"outcome": "same_page", "summary": "分析失败"}

    # ── 快照与回退 ───────────────────────────────────────────────────

    async def _save_snapshot(self, page: Page) -> dict:
        return {
            "url": page.url,
            "scroll_x": await page.evaluate("window.scrollX"),
            "scroll_y": await page.evaluate("window.scrollY"),
        }

    async def _restore_snapshot(self, page: Page, snapshot: dict):
        """恢复到快照前的 URL 和滚动位置"""
        target_url = snapshot["url"]
        if page.url != target_url:
            print_agent("page_explorer", f"    ⏪ 回退到: {target_url[:60]}")
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
        await asyncio.sleep(0.2)

    # ── 状态指纹 ─────────────────────────────────────────────────────

    async def _compute_state_hash(self, page: Page) -> str:
        """计算当前页面状态的一致性签名，用于去重"""
        try:
            url_base = page.url.split("?")[0]
            dom_digest = await page.evaluate("""() => {
                const text = document.body
                    ? document.body.innerText.substring(0, 500)
                    : '';
                return text.replace(/\\s+/g, ' ').trim();
            }""")
            raw = f"{url_base}||{dom_digest}"
            return hashlib.md5(raw.encode()).hexdigest()[:16]
        except Exception:
            return hashlib.md5(page.url.encode()).hexdigest()[:16]

    # ── 报告输出 ─────────────────────────────────────────────────────

    def _print_final_report(self):
        r = self.report
        print_success(
            f"\n{'='*60}\n"
            f"🎉 全量探索完成！\n"
            f"  ⏱️ 耗时:         {r.duration_seconds:.1f}s\n"
            f"  📄 探索节点数:   {r.total_nodes}\n"
            f"  🔀 探索边数:     {r.total_edges}\n"
            f"  🎯 发现元素总数: {r.total_elements_found}\n"
            f"  ✅ 已探索元素:   {r.total_elements_explored}\n"
            f"  ❌ 受阻元素:     {len(r.blocked_elements)}\n"
            f"{'='*60}"
        )

    def save_report(self, output_path: str):
        """将探索报告保存为 JSON 文件"""
        data = {
            "start_url": self.report.start_url,
            "duration_seconds": self.report.duration_seconds,
            "stats": {
                "total_nodes": self.report.total_nodes,
                "total_edges": self.report.total_edges,
                "elements_found": self.report.total_elements_found,
                "elements_explored": self.report.total_elements_explored,
                "blocked": len(self.report.blocked_elements),
            },
            "nodes": self.report.nodes,
            "edges": self.report.edges,
            "blocked_elements": self.report.blocked_elements,
        }
        Path(output_path).write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print_success(f"📊 探索报告已保存: {output_path}")
