"""
视觉感知引擎
通过截图 + 多模态大语言模型理解页面，取代传统的DOM解析
支持坐标智能精修：视觉坐标 → DOM元素吸附 → 多策略兜底
"""

from __future__ import annotations
import asyncio
import base64
import io
import json
import time
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

from webagent.prompt_engine.templates.vision import (
    VISION_PERCEIVE_PROMPT,
    VISION_VERIFY_PROMPT,
    VISION_LOCATE_PROMPT,
)
from webagent.utils.logger import get_logger, print_agent, print_warning
from webagent.utils.llm import get_config, get_llm

logger = get_logger("webagent.agents.vision_engine")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 坐标精修用的 JS 脚本
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 通过 elementFromPoint 找到坐标位置的真实 DOM 元素，返回其 bounding box 中心和元素信息
JS_REFINE_COORDINATE = """
(coords) => {
    const x = coords.x;
    const y = coords.y;
    const el = document.elementFromPoint(x, y);
    if (!el) return null;

    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const isInteractive = ['a', 'button', 'input', 'select', 'textarea', 'label', 'svg', 'canvas'].includes(tag)
        || el.getAttribute('role') === 'button'
        || el.getAttribute('role') === 'menuitem'
        || el.getAttribute('role') === 'tab'
        || el.getAttribute('role') === 'link'
        || type.toLowerCase() === 'submit'
        || (el.className && typeof el.className === 'string' && (el.className.toLowerCase().includes('login') || el.className.toLowerCase().includes('submit') || el.className.toLowerCase().includes('btn')))
        || (el.id && (el.id.toLowerCase().includes('login') || el.id.toLowerCase().includes('submit')))
        || el.onclick !== null
        || el.style.cursor === 'pointer'
        || window.getComputedStyle(el).cursor === 'pointer';

    // 如果命中的不是交互元素，向上查找最近的交互祖先
    let target = el;
    if (!isInteractive) {
        let parent = el.parentElement;
        for (let i = 0; i < 5 && parent; i++) {
            const ptag = parent.tagName.toLowerCase();
            const ptype = parent.getAttribute('type') || '';
            const pInteractive = ['a', 'button', 'input', 'select', 'textarea', 'svg', 'canvas'].includes(ptag)
                || parent.getAttribute('role') === 'button'
                || parent.getAttribute('role') === 'menuitem'
                || ptype.toLowerCase() === 'submit'
                || (parent.className && typeof parent.className === 'string' && (parent.className.toLowerCase().includes('login') || parent.className.toLowerCase().includes('submit') || parent.className.toLowerCase().includes('btn')))
                || (parent.id && (parent.id.toLowerCase().includes('login') || parent.id.toLowerCase().includes('submit')))
                || parent.onclick !== null
                || window.getComputedStyle(parent).cursor === 'pointer';
            if (pInteractive) {
                target = parent;
                break;
            }
            parent = parent.parentElement;
        }
    }

    const targetRect = target.getBoundingClientRect();
    const centerX = Math.round(targetRect.left + targetRect.width / 2);
    const centerY = Math.round(targetRect.top + targetRect.height / 2);

    return {
        original: { x: x, y: y },
        refined: { x: centerX, y: centerY },
        tag: target.tagName.toLowerCase(),
        id: target.id || '',
        text: (target.textContent || '').trim().substring(0, 80),
        role: target.getAttribute('role') || '',
        type: target.getAttribute('type') || '',
        href: target.getAttribute('href') || '',
        is_visible: targetRect.width > 0 && targetRect.height > 0,
        is_interactive: true,
        bounding_box: {
            x: Math.round(targetRect.x),
            y: Math.round(targetRect.y),
            width: Math.round(targetRect.width),
            height: Math.round(targetRect.height),
        },
        selector_hint: target.id ? '#' + target.id
            : (target.getAttribute('name') ? '[name=\"' + target.getAttribute('name') + '\"]' : ''),
    };
}
"""

# 在坐标附近的矩形区域内搜索所有可交互元素
JS_SCAN_NEARBY = """
(params) => {
    const cx = params.x;
    const cy = params.y;
    const radius = params.radius || 50;

    const results = [];
    const seen = new Set();

    // 在九宫格方向采样
    const offsets = [
        [0, 0], [-radius, 0], [radius, 0], [0, -radius], [0, radius],
        [-radius, -radius], [radius, -radius], [-radius, radius], [radius, radius],
        [-radius/2, 0], [radius/2, 0], [0, -radius/2], [0, radius/2],
    ];

    for (const [dx, dy] of offsets) {
        const px = cx + dx;
        const py = cy + dy;
        if (px < 0 || py < 0) continue;

        const el = document.elementFromPoint(px, py);
        if (!el) continue;

        // 向上找交互元素
        let target = el;
        let found = false;
        for (let node = el; node && node !== document.body; node = node.parentElement) {
            const tag = node.tagName.toLowerCase();
            const type = node.getAttribute('type') || '';
            if (['a', 'button', 'input', 'select', 'textarea', 'svg', 'canvas'].includes(tag)
                || node.getAttribute('role') === 'button'
                || node.getAttribute('role') === 'menuitem'
                || node.getAttribute('role') === 'tab'
                || type.toLowerCase() === 'submit'
                || (node.className && typeof node.className === 'string' && (node.className.toLowerCase().includes('login') || node.className.toLowerCase().includes('submit') || node.className.toLowerCase().includes('btn')))
                || (node.id && (node.id.toLowerCase().includes('login') || node.id.toLowerCase().includes('submit')))
                || node.onclick !== null
                || window.getComputedStyle(node).cursor === 'pointer') {
                target = node;
                found = true;
                break;
            }
        }
        if (!found) continue;

        // 去重
        const key = target.tagName + '_' + target.id + '_' + (target.textContent || '').trim().substring(0, 30);
        if (seen.has(key)) continue;
        seen.add(key);

        const rect = target.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;

        results.push({
            center_x: Math.round(rect.left + rect.width / 2),
            center_y: Math.round(rect.top + rect.height / 2),
            tag: target.tagName.toLowerCase(),
            text: (target.textContent || '').trim().substring(0, 80),
            id: target.id || '',
            distance: Math.sqrt((rect.left + rect.width/2 - cx) ** 2 + (rect.top + rect.height/2 - cy) ** 2),
            selector_hint: target.id ? '#' + target.id : '',
        });
    }

    // 按距离排序
    results.sort((a, b) => a.distance - b.distance);
    return results.slice(0, 5);
}
"""

JS_DRAW_SOM = """
(params) => {
    const prefix = params && params.prefix ? params.prefix : '';
    let som_idx = 1;
    window.__som_nodes__ = window.__som_nodes__ || [];
    if(window.__som_nodes__.length > 0) return {}; 
    const som_data = {};
    
    // 性能优化：只查询交互元素选择器，增加对于 login/submit 及前端库 svg 的兜底捕捉
    // Shadow DOM 限制最多 2 层深度
    const INTERACTIVE_SELECTOR = 'button, a, input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [tabindex]:not([tabindex="-1"]), .button, .btn, [type="submit"], [class*="login" i], [class*="submit" i], [id*="login" i], [id*="submit" i], svg, canvas';
    const elementsToMark = new Set();
    const traverse = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > 2) return;
        const els = root.querySelectorAll(INTERACTIVE_SELECTOR);
        els.forEach(e => elementsToMark.add(e));
        // 只对含 shadowRoot 的交互容器进入，不遍历全部 '*'
        const containers = root.querySelectorAll('[shadow], [data-shadow]');
        for (const el of containers) {
            if (el.shadowRoot) traverse(el.shadowRoot, depth + 1);
        }
    };
    traverse(document, 0);

    // 批量创建 fragment 减少 DOM reflow
    const fragment = document.createDocumentFragment();
    const vh = window.innerHeight;
    const vw = window.innerWidth;

    for (const el of elementsToMark) {
        const rect = el.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) continue;
        if (rect.bottom < 0 || rect.right < 0 || rect.top > vh || rect.left > vw) continue;
        
        // 使用 offsetParent 快速判断可见性，避免 getComputedStyle
        if (el.offsetParent === null && el.tagName.toLowerCase() !== 'body') continue;

        const idStr = prefix + String(som_idx++);
        el.setAttribute('data-som-id', idStr);

        const badge = document.createElement('div');
        badge.textContent = idStr;
        badge.style.cssText = `position:absolute;top:${rect.top + window.scrollY}px;left:${rect.left + window.scrollX}px;background:red;color:white;padding:1px 4px;font-size:12px;font-weight:bold;z-index:2147483647;pointer-events:none;border-radius:3px;border:1px solid white`;
        
        fragment.appendChild(badge);
        window.__som_nodes__.push(badge);
    }
    if (document.body) document.body.appendChild(fragment);
    return som_data;
}
"""

JS_DOM_STABLE_PROBE = """
() => {
    if (window.__mutation_observer_active) return;
    window.__is_stable = true;
    let timeoutId;
    const observer = new MutationObserver(() => {
        window.__is_stable = false;
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
            window.__is_stable = true;
        }, 300);
    });
    if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true, attributes: true });
        window.__mutation_observer_active = true;
    }
}
"""


JS_CLEAR_SOM = """
() => {
    if (window.__som_nodes__) {
        for (const node of window.__som_nodes__) {
            if (node.parentElement) node.parentElement.removeChild(node);
        }
        window.__som_nodes__ = [];
    }
}
"""



@dataclass
class VisionAction:
    """视觉模型推理出的下一步操作"""
    action_type: str        # click, fill, scroll_down, scroll_up, hover, select, ...
    target_description: str # "顶部导航栏的用户管理菜单"
    coordinates: dict       # {"x": 340, "y": 220}
    value: str = ""         # fill 操作时的输入值
    reasoning: str = ""     # 推理原因
    page_description: str = ""
    visible_elements: list = field(default_factory=list)
    is_dead_end: bool = False
    dead_end_reason: str = ""
    selector_hint: str = "" # CSS 选择器提示（精修后可能获得）
    element_id: str = ""    # SOM 标记的独立ID，优先于坐标
    risk_level: str = "safe" # safe | dangerous


@dataclass
class VerifyResult:
    """操作验证结果"""
    success: bool
    page_changed: bool = False
    change_description: str = ""
    error_detected: bool = False
    error_message: str = ""
    suggestion: str = ""


class VisionEngine:
    """
    视觉感知引擎 — 智能体的"眼睛"

    坐标精修策略（三层兜底）:
      Layer 1: elementFromPoint(x,y) → 获取真实元素的 bounding box 中心
      Layer 2: 如果L1命中了非交互元素 → 沿 DOM 树上溯找可交互祖先
      Layer 3: 如果L1+L2都失败 → 在坐标附近50px半径内扫描可交互元素
    """

    def __init__(self, screenshots_dir: str = "screenshots"):
        self.config = get_config()
        self.llm = None  # 延迟初始化
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def _get_llm(self):
        if self.llm is None:
            self.llm = get_llm()
        return self.llm

    @staticmethod
    async def _wait_stable(page: Page, timeout: int = 3000):
        """智能等待页面 DOM 稳定，代替硬编码 sleep"""
        try:
            # 注入稳定探针（幂等）
            for frame in page.frames:
                try:
                    await frame.evaluate(JS_DOM_STABLE_PROBE)
                except Exception:
                    pass
            # 等待 DOM 稳定标志位
            await page.wait_for_function("window.__is_stable === true", timeout=timeout)
        except Exception:
            # 降级：最短等待
            await asyncio.sleep(0.3)

    # ── 截图压缩参数 ──
    SCREENSHOT_QUALITY = 70       # JPEG 质量 (1-100)
    SCREENSHOT_MAX_WIDTH = 1024   # 最大宽度像素

    async def _screenshot(self, page: Page, label: str = "", draw_som: bool = False) -> str:
        """截图并返回压缩后的 JPEG 文件路径。如果开启 draw_som，则会在截图前先叠加数字标识标签"""
        ts = int(time.time() * 1000)
        png_filename = f"{label}_{ts}.png" if label else f"screenshot_{ts}.png"
        png_path = self.screenshots_dir / png_filename
        
        if draw_som:
            try:
                for idx, frame in enumerate(page.frames):
                    try:
                        prefix = "" if idx == 0 else f"{idx}_"
                        await frame.evaluate(JS_DRAW_SOM, {"prefix": prefix})
                    except Exception:
                        pass
                # 用 requestAnimationFrame 等待渲染完成，代替固定 sleep
                try:
                    await page.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                except Exception:
                    await asyncio.sleep(0.05)
            except Exception as e:
                logger.debug(f"Draw SOM 失败: {e}")

        await page.screenshot(path=str(png_path), full_page=False)

        if draw_som:
            try:
                for frame in page.frames:
                    try:
                        await frame.evaluate(JS_CLEAR_SOM)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Clear SOM 失败: {e}")

        # ── PNG → 压缩 JPEG（减少 5~10 倍体积）──
        jpeg_path = self._compress_screenshot(str(png_path))
        return jpeg_path

    def _compress_screenshot(self, png_path: str) -> str:
        """将 PNG 截图压缩为 JPEG 并缩放到合理尺寸"""
        try:
            from PIL import Image
            img = Image.open(png_path)

            # 缩放到 max_width
            if img.width > self.SCREENSHOT_MAX_WIDTH:
                ratio = self.SCREENSHOT_MAX_WIDTH / img.width
                new_size = (self.SCREENSHOT_MAX_WIDTH, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 转 RGB（JPEG 不支持 RGBA）
            if img.mode == 'RGBA':
                img = img.convert('RGB')

            jpeg_path = png_path.replace('.png', '.jpg')
            img.save(jpeg_path, 'JPEG', quality=self.SCREENSHOT_QUALITY, optimize=True)

            # 删除原始 PNG 节省磁盘空间
            try:
                Path(png_path).unlink()
            except Exception:
                pass

            return jpeg_path
        except ImportError:
            logger.debug("Pillow 未安装，使用原始 PNG")
            return png_path
        except Exception as e:
            logger.debug(f"截图压缩失败: {e}")
            return png_path

    def _image_to_base64(self, image_path: str) -> str:
        """将截图转为 base64"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    async def _call_vision_llm(self, prompt: str, image_paths: list[str]) -> str:
        """调用多模态 LLM（支持 Claude / GPT-4o / Gemini 的图片输入）"""
        from langchain_core.messages import HumanMessage

        llm = self._get_llm()

        content = []
        for img_path in image_paths:
            b64 = self._image_to_base64(img_path)
            # 自动检测图片格式
            mime = "image/jpeg" if img_path.endswith(".jpg") else "image/png"
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                },
            })
        content.append({
            "type": "text",
            "text": prompt,
        })

        response = await llm.ainvoke([HumanMessage(content=content)])
        return response.content

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 响应中提取 JSON"""
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                return json.loads(text[json_start:json_end])
            except json.JSONDecodeError:
                pass
        return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 坐标精修（核心优化）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _refine_coordinates(
        self,
        page: Page,
        x: int,
        y: int,
        target_description: str = "",
    ) -> tuple[int, int, str, str]:
        """
        坐标精修：将视觉模型给的粗略坐标精修为真实 DOM 元素的中心点

        策略:
          1. elementFromPoint(x,y) → 吸附到真实元素中心
          2. 向上查找可交互祖先（按钮、链接等）
          3. 附近区域扫描

        Returns:
            (refined_x, refined_y, selector_hint, method_used)
        """
        # ── Layer 1 + 2: elementFromPoint + 祖先上溯 ──
        try:
            result = await page.evaluate(JS_REFINE_COORDINATE, {"x": x, "y": y})
            if result and result.get("is_visible"):
                rx = result["refined"]["x"]
                ry = result["refined"]["y"]
                selector = result.get("selector_hint", "")
                tag = result.get("tag", "")
                text = result.get("text", "")[:40]

                # 检查精修后的偏移量
                offset = ((rx - x) ** 2 + (ry - y) ** 2) ** 0.5
                if offset > 0:
                    logger.info(
                        f"坐标精修: ({x},{y}) → ({rx},{ry}) "
                        f"[{tag}] \"{text}\" (偏移 {offset:.0f}px)"
                    )
                    print_agent("vision", f"  🎯 坐标精修: ({x},{y}) → ({rx},{ry}) [{tag}] \"{text}\"")
                return rx, ry, selector, "refine"
        except Exception as e:
            logger.debug(f"坐标精修 L1 失败: {e}")

        # ── Layer 3: 附近区域扫描 ──
        try:
            nearby = await page.evaluate(JS_SCAN_NEARBY, {"x": x, "y": y, "radius": 50})
            if nearby and len(nearby) > 0:
                # 优先找文本匹配的
                if target_description:
                    desc_lower = target_description.lower()
                    for item in nearby:
                        item_text = item.get("text", "").lower()
                        if any(kw in item_text for kw in desc_lower.split()[:3]):
                            nx, ny = item["center_x"], item["center_y"]
                            print_agent("vision",
                                f"  🔍 附近扫描命中: ({x},{y}) → ({nx},{ny}) "
                                f"[{item['tag']}] \"{item['text'][:30]}\" (距离 {item['distance']:.0f}px)")
                            return nx, ny, item.get("selector_hint", ""), "scan_text"

                # 没有文本匹配则用最近的
                best = nearby[0]
                nx, ny = best["center_x"], best["center_y"]
                print_agent("vision",
                    f"  🔍 附近最近元素: ({x},{y}) → ({nx},{ny}) "
                    f"[{best['tag']}] \"{best['text'][:30]}\" (距离 {best['distance']:.0f}px)")
                return nx, ny, best.get("selector_hint", ""), "scan_nearest"
        except Exception as e:
            logger.debug(f"附近扫描失败: {e}")

        # 所有策略都失败，返回原始坐标
        logger.info(f"坐标精修未找到元素，使用原始坐标 ({x},{y})")
        return x, y, "", "raw"

    async def _smart_click(self, page: Page, x: int, y: int, selector: str = "") -> bool:
        """
        智能点击：优先用 selector，用不了再用精修坐标

        Returns:
            是否成功点击
        """
        # 策略1: 如果有 CSS 选择器，优先使用 Playwright 的元素级点击
        if selector:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.click(timeout=3000)
                    print_agent("vision", f"  ✅ 选择器点击成功: {selector}")
                    return True
            except Exception as e:
                logger.debug(f"选择器点击失败 [{selector}]: {e}")

        # 策略2: 使用精修后的坐标点击
        try:
            await page.mouse.click(x, y)
            return True
        except Exception as e:
            logger.debug(f"坐标点击失败 ({x},{y}): {e}")

        # 策略3: 用 JS 直接触发点击事件
        try:
            clicked = await page.evaluate(f"""
                (() => {{
                    const el = document.elementFromPoint({x}, {y});
                    if (el) {{
                        el.click();
                        return true;
                    }}
                    return false;
                }})()
            """)
            if clicked:
                print_agent("vision", f"  ✅ JS 点击兜底成功")
                return True
        except Exception as e:
            logger.debug(f"JS 点击失败: {e}")

        return False

    async def _smart_fill(self, page: Page, x: int, y: int, value: str, selector: str = "") -> bool:
        """
        智能填写：优先用 selector 定位输入框
        """
        # 策略1: 选择器
        if selector:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await locator.first.click(click_count=3, timeout=3000)
                    await page.keyboard.press("Backspace")
                    await locator.first.fill(value)
                    print_agent("vision", f"  ✅ 选择器填写成功: {selector}")
                    return True
            except Exception as e:
                logger.debug(f"选择器填写失败 [{selector}]: {e}")

        # 策略2: 先找到坐标位置的输入框元素
        try:
            # 用 elementFromPoint 找到元素，检查它是否是 input/textarea
            el_info = await page.evaluate("""
                (coords) => {
                    const el = document.elementFromPoint(coords.x, coords.y);
                    if (!el) return null;
                    // 如果不是输入框，看看里面有没有输入框
                    let target = el;
                    const tag = el.tagName.toLowerCase();
                    if (!['input', 'textarea', 'select'].includes(tag)) {
                        const inner = el.querySelector('input, textarea');
                        if (inner) target = inner;
                    }
                    const ttag = target.tagName.toLowerCase();
                    if (['input', 'textarea'].includes(ttag)) {
                        const rect = target.getBoundingClientRect();
                        return {
                            tag: ttag,
                            id: target.id || '',
                            name: target.getAttribute('name') || '',
                            center_x: Math.round(rect.left + rect.width / 2),
                            center_y: Math.round(rect.top + rect.height / 2),
                        };
                    }
                    return null;
                }
            """, {"x": x, "y": y})

            if el_info:
                # 构建选择器
                fill_selector = ""
                if el_info.get("id"):
                    fill_selector = f"#{el_info['id']}"
                elif el_info.get("name"):
                    fill_selector = f"[name='{el_info['name']}']"

                if fill_selector:
                    locator = page.locator(fill_selector)
                    await locator.first.click(click_count=3, timeout=3000)
                    await page.keyboard.press("Backspace")
                    await locator.first.fill(value)
                    print_agent("vision", f"  ✅ 精修后填写成功: {fill_selector}")
                    return True
        except Exception as e:
            logger.debug(f"精修填写失败: {e}")

        # 策略3: 坐标点击后键盘输入
        try:
            await page.mouse.click(x, y, click_count=3)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Backspace")
            await page.keyboard.type(value, delay=30)
            return True
        except Exception as e:
            logger.debug(f"坐标键盘输入失败: {e}")
            return False

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 核心方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def perceive(
        self,
        page: Page,
        goal: str,
        action_history: list[str] | None = None,
    ) -> VisionAction:
        """
        感知：截图 → 视觉模型推理下一步操作
        """
        screenshot_path = await self._screenshot(page, "perceive", draw_som=True)
        print_agent("vision", f"📸 感知截图 (带分析标签): {screenshot_path}")

        history_text = "\n".join(
            f"  {i+1}. {a}" for i, a in enumerate(action_history or [])
        ) or "（尚未执行任何操作）"

        prompt = VISION_PERCEIVE_PROMPT.format(
            goal=goal,
            action_history=history_text,
        )

        try:
            response = await self._call_vision_llm(prompt, [screenshot_path])
            data = self._extract_json(response)

            if data and "next_action" in data:
                na = data["next_action"]
                return VisionAction(
                    action_type=na.get("action_type", "click"),
                    target_description=na.get("target_description", ""),
                    coordinates=na.get("coordinates", {"x": 0, "y": 0}),
                    value=na.get("value", ""),
                    reasoning=na.get("reasoning", ""),
                    page_description=data.get("page_description", ""),
                    visible_elements=data.get("visible_elements", []),
                    is_dead_end=data.get("is_dead_end", False),
                    dead_end_reason=data.get("dead_end_reason", ""),
                    selector_hint=na.get("selector_hint", ""),
                    element_id=str(na.get("element_id", "")),
                    risk_level=na.get("risk_level", "safe"),
                )
        except Exception as e:
            logger.warning(f"视觉感知失败: {e}")

        return VisionAction(
            action_type="none",
            target_description="",
            coordinates={"x": 0, "y": 0},
            is_dead_end=True,
            dead_end_reason=f"视觉感知异常",
        )

    async def quick_verify(
        self,
        page: Page,
        snapshot_before: dict,
        action_description: str,
    ) -> VerifyResult | None:
        """
        轻量级快速验证：基于 URL 变化和 DOM 内容变化快速判断操作是否生效。
        无需 LLM 调用。返回 None 表示不确定，需要回退到 LLM verify。
        """
        try:
            url_changed = page.url != snapshot_before.get("url", "")
            title = await page.title()
            title_changed = title != snapshot_before.get("title", "")

            # 快速 DOM 内容指纹：文本长度 + 可见元素数量
            content_hash = await page.evaluate("""
                () => {
                    const text_len = (document.body.innerText || '').length;
                    const elements = document.querySelectorAll('button, a, input, select, textarea');
                    return text_len * 1000 + elements.length;
                }
            """)
            content_changed = content_hash != snapshot_before.get("content_hash", 0)

            # 检查是否有错误弹窗
            has_error = await page.evaluate("""
                () => {
                    const errorSels = '.ant-message-error, .el-message--error, .alert-danger, .error-message, .toast-error';
                    const el = document.querySelector(errorSels);
                    return el ? el.textContent.trim().substring(0, 100) : '';
                }
            """)
            if has_error:
                return VerifyResult(
                    success=False, page_changed=False,
                    change_description=f"检测到错误: {has_error}",
                    error_detected=True, error_message=has_error,
                )

            if url_changed or title_changed or content_changed:
                change_desc = []
                if url_changed:
                    change_desc.append(f"URL→{page.url}")
                if title_changed:
                    change_desc.append(f"标题→{title}")
                if content_changed:
                    change_desc.append("内容已变化")
                return VerifyResult(
                    success=True, page_changed=url_changed,
                    change_description="; ".join(change_desc),
                )

            # 无变化 → 不确定，返回 None 让调用方决定是否用 LLM verify
            return None
        except Exception as e:
            logger.debug(f"快速验证异常: {e}")
            return None

    async def get_page_snapshot(self, page: Page) -> dict:
        """获取页面快照指纹，用于 quick_verify 对比"""
        try:
            content_hash = await page.evaluate("""
                () => {
                    const text_len = (document.body.innerText || '').length;
                    const elements = document.querySelectorAll('button, a, input, select, textarea');
                    return text_len * 1000 + elements.length;
                }
            """)
            return {
                "url": page.url,
                "title": await page.title(),
                "content_hash": content_hash,
            }
        except Exception:
            return {"url": page.url, "title": "", "content_hash": 0}

    async def verify(
        self,
        page: Page,
        screenshot_before: str,
        action_description: str,
    ) -> tuple[VerifyResult, str]:
        """验证：对比操作前后截图，判断操作是否成功（LLM 版本，较慢）"""
        screenshot_after = await self._screenshot(page, "verify")
        print_agent("vision", f"📸 验证截图: {screenshot_after}")

        prompt = VISION_VERIFY_PROMPT.format(
            action_description=action_description,
        )

        try:
            response = await self._call_vision_llm(
                prompt, [screenshot_before, screenshot_after]
            )
            data = self._extract_json(response)

            if data:
                return VerifyResult(
                    success=data.get("success", False),
                    page_changed=data.get("page_changed", False),
                    change_description=data.get("change_description", ""),
                    error_detected=data.get("error_detected", False),
                    error_message=data.get("error_message", ""),
                    suggestion=data.get("suggestion", ""),
                ), screenshot_after
        except Exception as e:
            logger.warning(f"视觉验证失败: {e}")

        return VerifyResult(
            success=False,
            change_description="视觉验证异常",
        ), screenshot_after

    async def locate_element(
        self,
        page: Page,
        element_description: str,
    ) -> tuple[int, int, float]:
        """视觉定位：找到指定元素的坐标"""
        screenshot_path = await self._screenshot(page, "locate")

        prompt = VISION_LOCATE_PROMPT.format(
            element_description=element_description,
        )

        try:
            response = await self._call_vision_llm(prompt, [screenshot_path])
            data = self._extract_json(response)

            if data and data.get("found"):
                coords = data.get("coordinates", {})
                return (
                    int(coords.get("x", 0)),
                    int(coords.get("y", 0)),
                    float(data.get("confidence", 0.5)),
                )
        except Exception as e:
            logger.warning(f"视觉定位失败: {e}")

        return (0, 0, 0.0)

    async def execute_vision_action(
        self,
        page: Page,
        action: VisionAction,
    ) -> bool:
        """
        执行视觉操作（带坐标精修 + 多策略兜底）

        流程:
          1. 视觉模型给出粗略坐标 (x, y)
          2. _refine_coordinates: elementFromPoint → 吸附到真实元素中心
          3. _smart_click / _smart_fill: 选择器优先 → 精修坐标 → JS 兜底
        """
        raw_x = action.coordinates.get("x", 0)
        raw_y = action.coordinates.get("y", 0)
        action_type = action.action_type

        if action_type in ("none",):
            return False

        # 对需要精准定位的操作，先执行坐标精修
        # 使用 DOM 稳定探针代替硬编码 sleep
        async def robust_wait(pg: Page):
            await VisionEngine._wait_stable(pg)

        # 优先使用 SOM ID 进行绝对精确点击
        selector = ""
        if action.element_id:
            selector = f'[data-som-id="{action.element_id}"]'

        if not selector and action_type in ("click", "double_click", "right_click", "fill", "select", "hover"):
            x, y, selector_refined, method = await self._refine_coordinates(
                page, raw_x, raw_y, action.target_description
            )
            # 合并已知的 selector 提示
            selector = selector_refined or action.selector_hint
        else:
            x, y = raw_x, raw_y

        try:
            if action_type == "click":
                success = await self._smart_click(page, x, y, selector)
                await robust_wait(page)
                return success

            elif action_type == "double_click":
                if selector:
                    try:
                        locator = page.locator(selector)
                        if await locator.count() > 0:
                            await locator.first.dblclick(timeout=3000)
                            await robust_wait(page)
                            return True
                    except Exception:
                        pass
                await page.mouse.dblclick(x, y)
                await robust_wait(page)
                return True

            elif action_type == "right_click":
                await page.mouse.click(x, y, button="right")
                await robust_wait(page)
                return True

            elif action_type == "hover":
                await page.mouse.move(x, y)
                return True

            elif action_type == "fill":
                success = await self._smart_fill(page, x, y, action.value, selector)
                await robust_wait(page)
                return success

            elif action_type == "select":
                # 先点击展开下拉框
                click_ok = await self._smart_click(page, x, y, selector)
                if not click_ok:
                    return False
                await robust_wait(page)

                if action.value:
                    # 尝试在展开的列表中找到目标选项
                    found_option = await page.evaluate(f"""
                        (() => {{
                            const options = document.querySelectorAll(
                                '.ant-select-item, .el-select-dropdown__item, '
                                + '[role="option"], [role="listbox"] li, '
                                + '.dropdown-item, .dropdown-menu li, option'
                            );
                            for (const opt of options) {{
                                if (opt.textContent.trim().includes('{action.value}')) {{
                                    opt.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }})()
                    """)
                    if found_option:
                        print_agent("vision", f"  ✅ 下拉选项定位成功: {action.value}")
                        await robust_wait(page)
                        return True

                    # 兜底：键盘输入搜索
                    await page.keyboard.type(action.value, delay=30)
                    await robust_wait(page)
                    await page.keyboard.press("Enter")
                    await robust_wait(page)

                return True

            elif action_type == "scroll_down":
                await page.mouse.wheel(0, 300)
                await robust_wait(page)
                return True

            elif action_type == "scroll_up":
                await page.mouse.wheel(0, -300)
                await robust_wait(page)
                return True

            else:
                logger.warning(f"未知的视觉操作类型: {action_type}")
                return False

        except Exception as e:
            logger.warning(f"视觉操作执行失败: {e}")
            return False
