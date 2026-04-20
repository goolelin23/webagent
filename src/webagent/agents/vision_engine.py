"""
视觉感知引擎
通过截图 + 多模态大语言模型理解页面，取代传统的DOM解析
支持坐标智能精修：视觉坐标 → DOM元素吸附 → 多策略兜底
"""

from __future__ import annotations
import asyncio
import base64
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
    VISION_ZOOM_REFINE_PROMPT,
    VISION_SOM_FALLBACK_PROMPT,
)
from webagent.utils.logger import get_logger, print_agent, print_warning
from webagent.utils.llm import get_config, get_llm

logger = get_logger("webpilot.agents.vision_engine")

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

JS_DRAW_MISSING_SOM = """
(coords) => {
    window.__som_nodes__ = window.__som_nodes__ || [];
    let som_idx = document.querySelectorAll('[data-som-id]').length + 1;

    // --- Helper: 深度元素获取 (支持 Shadow DOM) ---
    const getDeepElement = (x, y, root = document) => {
        let el = root.elementFromPoint(x, y);
        if (el && el.shadowRoot) {
            const inner = getDeepElement(x, y, el.shadowRoot);
            return inner || el;
        }
        return el;
    };

    // --- Helper: 交互特性检查 ---
    const isInteractive = (el) => {
        if (!el) return false;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role');
        const style = window.getComputedStyle(el);
        return (
            ['button', 'a', 'input', 'select', 'textarea'].includes(tag) ||
            ['button', 'link', 'menuitem', 'tab', 'checkbox'].includes(role) ||
            style.cursor === 'pointer' ||
            el.onclick !== null ||
            (el.className && typeof el.className === 'string' && /btn|button|action|item/i.test(el.className))
        );
    };

    // --- Helper: 向上溯源寻找“有意义”的容器 ---
    const findInteractiveShell = (el) => {
        let curr = el;
        for (let i = 0; i < 5 && curr && curr !== document.body; i++) {
            if (isInteractive(curr)) return curr;
            curr = curr.parentElement;
        }
        return el;
    };

    for (let pt of coords) {
        let bestTarget = getDeepElement(pt.x, pt.y);
        let foundViaTrack = 'none';
        
        // --- Track A/B: 强化吸附 (文本 + 结构特征综合扫描) ---
        const radius = 60;
        const step = 20;
        let candidates = [];

        for (let dx = -radius; dx <= radius; dx += step) {
            for (let dy = -radius; dy <= radius; dy += step) {
                const el = getDeepElement(pt.x + dx, pt.y + dy);
                if (!el || el === document.body) continue;

                let score = 0;
                const txt = (el.textContent || '').replace(/\s+/g, '').toLowerCase();
                const desc = (pt.desc || '').toLowerCase().replace(/\s+/g, '');

                // 评分机制
                if (desc && txt && (desc.includes(txt) || txt.includes(desc)) && txt.length > 0 && txt.length < 30) {
                    score += 100; // 文本命中
                }
                if (['svg', 'img', 'canvas'].includes(el.tagName.toLowerCase())) {
                    score += 50; // 视觉素材命中
                }
                if (isInteractive(el)) {
                    score += 40; // 交互属性命中
                }

                if (score > 0) {
                    candidates.push({ el: el, score: score, dist: dx*dx + dy*dy });
                }
            }
        }

        if (candidates.length > 0) {
            // 优先选高分，同分选最近
            candidates.sort((a, b) => b.score - a.score || a.dist - b.dist);
            bestTarget = candidates[0].el;
        }

        // --- 最终处理：向上找到真正的点击壳子 ---
        const target = findInteractiveShell(bestTarget || document.body);
        
        if (target.hasAttribute('data-som-id') || target.closest('[data-som-id]')) continue;

        const idStr = 'V' + som_idx++;
        target.setAttribute('data-som-id', idStr);
        
        const rect = target.getBoundingClientRect();
        const badge = document.createElement('div');
        badge.textContent = idStr;
        
        let top = pt.y - 10;
        let left = pt.x - 10;
        if (rect.width > 0 && rect.height > 0) {
            // 徽章防止遮挡：放在左上角内侧
            top = Math.max(0, rect.top);
            left = Math.max(0, rect.left);
        }
        
        badge.style.cssText = `position:absolute;top:${top + window.scrollY}px;left:${left + window.scrollX}px;background:#3b82f6;color:white;padding:1px 4px;font-size:12px;font-weight:bold;z-index:2147483647;pointer-events:none;border-radius:3px;border:1px solid white;box-shadow: 0 2px 4px rgba(0,0,0,0.3);`;
        
        document.body.appendChild(badge);
        window.__som_nodes__.push(badge);
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
    
    # [Self-Repair Mechanism] 全局视觉自愈偏移量
    # 在遇到人工接管纠错时，持续测量 LLM 的输出偏差并动态调节
    GLOBAL_OFFSET_X: int = 0
    GLOBAL_OFFSET_Y: int = 0

    @classmethod
    def update_global_offset(cls, dx: int, dy: int):
        """动态修正视觉偏移误差（指数平滑更新）"""
        if cls.GLOBAL_OFFSET_X == 0 and cls.GLOBAL_OFFSET_Y == 0:
            cls.GLOBAL_OFFSET_X = dx
            cls.GLOBAL_OFFSET_Y = dy
        else:
            # 使用类似于梯度下降的平滑策略(EMA)，防止由于用户的某次手抖异常点击导致全局飞偏
            cls.GLOBAL_OFFSET_X = int(cls.GLOBAL_OFFSET_X * 0.7 + dx * 0.3)
            cls.GLOBAL_OFFSET_Y = int(cls.GLOBAL_OFFSET_Y * 0.7 + dy * 0.3)
        print_warning(f"✨ 视觉归一化网络触突已演进更新 -> 新全局偏移预测补充值: (X: {cls.GLOBAL_OFFSET_X}px, Y: {cls.GLOBAL_OFFSET_Y}px)")

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

            # === 视觉兜底两段式标记 (Vision-based SOM Fallback) ===
            try:
                stage_png = str(self.screenshots_dir / f"stage1_{ts}.png")
                await page.screenshot(path=stage_png, full_page=False)
                
                resp = await self._call_vision_llm(VISION_SOM_FALLBACK_PROMPT, [stage_png])
                data = self._extract_json(resp)
                
                if data and "missing_elements" in data and len(data["missing_elements"]) > 0:
                    vw = await page.evaluate("window.innerWidth")
                    vh = await page.evaluate("window.innerHeight")
                    coords = []
                    for el in data["missing_elements"]:
                        x = int(el.get("x_percent", 0) * vw)
                        y = int(el.get("y_percent", 0) * vh)
                        if x > 0 and y > 0:
                            coords.append({"x": x, "y": y, "desc": el.get("description", "")})
                    
                    if coords:
                        print_agent("vision", f"  🔍 视觉查漏检出 {len(coords)} 个被纯 DOM 遗漏的交互组件，在此处注入蓝色标签 (V系列)...")
                        await page.evaluate(JS_DRAW_MISSING_SOM, coords)
                        try:
                            await page.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                        except Exception:
                            await asyncio.sleep(0.05)
                            
                try:
                    Path(stage_png).unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"视觉两段式查漏失败: {e}")
            # ======================================================

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

    async def _scale_llm_coords(self, page: Page, llm_x: int, llm_y: int, screenshot_path: str) -> tuple[int, int]:
        """将 LLM 相对于截图尺寸返回的坐标缩放回 Playwright 使用的纯正 CSS 逻辑视口像素，并应用全局自愈补偿"""
        try:
            from PIL import Image
            with Image.open(screenshot_path) as img:
                img_w, img_h = img.size
            viewport = await page.evaluate("() => { return { w: window.innerWidth, h: window.innerHeight }; }")
            vw, vh = int(viewport["w"]), int(viewport["h"])
            x = int((llm_x / img_w) * vw)
            y = int((llm_y / img_h) * vh)
            logger.debug(f"📐 视觉底层坐标映射: LLM({llm_x},{llm_y}) [图片 {img_w}x{img_h}] -> CSS({x},{y}) [视口 {vw}x{vh}]")
        except Exception as e:
            logger.warning(f"坐标缩放转换异常: {e}，将使用原始坐标")
            x, y = llm_x, llm_y

        # 应用全局自演进修复偏移量
        x += self.GLOBAL_OFFSET_X
        y += self.GLOBAL_OFFSET_Y
        return x, y

    async def _zoom_refine_coords(
        self,
        page: Page,
        rough_css_x: int,
        rough_css_y: int,
        element_description: str,
        zoom_radius: int = 320,
        zoom_output_size: int = 512,
    ) -> tuple[int, int, float]:
        """
        视觉二阶精修（Visual Zoom-In Refinement）

        流程:
          1. 全图截图  → Playwright 原始 PNG（不压缩）
          2. 坐标映射  → CSS viewport 坐标 × devicePixelRatio → 截图物理像素坐标
          3. 裁剪局部  → 以粗略坐标为中心裁剪 zoom_radius×2 的区域
          4. 等比放大  → 贴合 zoom_output_size×zoom_output_size（letterbox 黑边）
          5. 局部精定位 → 调用视觉模型在放大图中返回 rx/ry
          6. 坐标反算  → 放大图坐标 → 裁剪图坐标 → 全图像素坐标 → CSS viewport 坐标
          7. 边界保护  → clamp 到 viewport 范围内

        返回 (refined_css_x, refined_css_y, confidence)
        失败时降级返回 (rough_css_x, rough_css_y, 0.0)
        """
        from PIL import Image
        import io

        # ── Step 1: 全图截图（PNG 原始，不再压缩，保留每一个物理像素）──
        ts = int(time.time() * 1000)
        png_path = self.screenshots_dir / f"zoom_full_{ts}.png"
        await page.screenshot(path=str(png_path), full_page=False)

        try:
            # ── Step 2: 坐标映射 — CSS px → 截图物理像素 ──
            viewport_info = await page.evaluate(
                """() => ({
                    w: window.innerWidth,
                    h: window.innerHeight,
                    dpr: window.devicePixelRatio || 1
                })"""
            )
            vw  = int(viewport_info["w"])
            vh  = int(viewport_info["h"])
            dpr = float(viewport_info["dpr"])

            with Image.open(str(png_path)) as full_img:
                full_w, full_h = full_img.size

            # 如果 Playwright 以物理像素保存（HDPI），则需乘以 DPR
            # 保险起见用实际图片宽度推算比例，而不是硬乘 DPR
            scale_x = full_w / vw   # 截图物理宽度 / CSS 宽度
            scale_y = full_h / vh

            px_x = int(rough_css_x * scale_x)  # 粗略坐标的截图物理像素位置
            px_y = int(rough_css_y * scale_y)

            logger.debug(
                f"🔍 Zoom Refine — CSS({rough_css_x},{rough_css_y}) "
                f"DPR={dpr:.1f} Scale=({scale_x:.2f},{scale_y:.2f}) "
                f"--> 物理像素({px_x},{px_y}) [全图 {full_w}x{full_h}]"
            )

            # ── Step 3: 裁剪局部 ──
            # 以物理像素为单位的裁剪半径 = zoom_radius * scale
            phys_radius = int(zoom_radius * max(scale_x, scale_y))
            crop_x1 = max(0,      px_x - phys_radius)
            crop_y1 = max(0,      px_y - phys_radius)
            crop_x2 = min(full_w, px_x + phys_radius)
            crop_y2 = min(full_h, px_y + phys_radius)

            # 裁剪区域真实宽高
            crop_w = crop_x2 - crop_x1
            crop_h = crop_y2 - crop_y1

            with Image.open(str(png_path)) as full_img:
                cropped = full_img.crop((crop_x1, crop_y1, crop_x2, crop_y2))

            # ── Step 4: 等比放大到 zoom_output_size（letterbox 黑边）──
            ratio = min(zoom_output_size / crop_w, zoom_output_size / crop_h)
            new_w = int(crop_w * ratio)
            new_h = int(crop_h * ratio)
            resized = cropped.resize((new_w, new_h), Image.LANCZOS)

            canvas = Image.new("RGB", (zoom_output_size, zoom_output_size), (0, 0, 0))
            paste_x = (zoom_output_size - new_w) // 2
            paste_y = (zoom_output_size - new_h) // 2
            canvas.paste(resized, (paste_x, paste_y))

            zoom_path = self.screenshots_dir / f"zoom_crop_{ts}.jpg"
            canvas.save(str(zoom_path), "JPEG", quality=90)

            logger.debug(
                f"🔬 裁剪区域: ({crop_x1},{crop_y1})-({crop_x2},{crop_y2}) "
                f"→ 放大到 {zoom_output_size}x{zoom_output_size} (paste@{paste_x},{paste_y}, {new_w}x{new_h})"
            )

            # ── Step 5: 局部精定位 — 调用 VLM 在放大图中定位 ──
            prompt = VISION_ZOOM_REFINE_PROMPT.format(element_description=element_description)
            response = await self._call_vision_llm(prompt, [str(zoom_path)])
            data = self._extract_json(response)

            if not (data and data.get("found")):
                logger.info(f"Zoom Refine: VLM 在局部截图中未找到元素，降级使用粗略坐标")
                return rough_css_x, rough_css_y, 0.0

            zoom_coords = data.get("coordinates", {})
            zoom_x = int(zoom_coords.get("x", 0))
            zoom_y = int(zoom_coords.get("y", 0))
            confidence = float(data.get("confidence", 0.0))

            # ── Step 6: 坐标反算 ──
            # 6a. 放大图坐标 → canvas 偏移去除 → 裁剪图坐标（物理像素）
            local_x = (zoom_x - paste_x) / ratio   # 裁剪图内的物理像素坐标
            local_y = (zoom_y - paste_y) / ratio

            # 6b. 裁剪图坐标 → 全图物理像素坐标
            global_px_x = crop_x1 + local_x
            global_px_y = crop_y1 + local_y

            # 6c. 全图物理像素坐标 → CSS viewport 逻辑像素
            refined_css_x = int(global_px_x / scale_x)
            refined_css_y = int(global_px_y / scale_y)

            # ── Step 7: 边界保护 ──
            refined_css_x = max(0, min(vw, refined_css_x))
            refined_css_y = max(0, min(vh, refined_css_y))

            # 应用全局自愈偏移
            refined_css_x += self.GLOBAL_OFFSET_X
            refined_css_y += self.GLOBAL_OFFSET_Y

            offset_from_rough = ((refined_css_x - rough_css_x)**2 + (refined_css_y - rough_css_y)**2) ** 0.5
            print_agent(
                "vision",
                f"  🔬 二阶精修完成: 粗略({rough_css_x},{rough_css_y}) → "
                f"精准({refined_css_x},{refined_css_y}) "
                f"Δ={offset_from_rough:.0f}px 置信度={confidence:.0%}"
            )
            return refined_css_x, refined_css_y, confidence

        except Exception as e:
            logger.warning(f"Zoom Refine 失败: {e}，降级使用粗略坐标")
            return rough_css_x, rough_css_y, 0.0
        finally:
            # 清理全图截图（局部截图保留用于 debug）
            try:
                png_path.unlink(missing_ok=True)
            except Exception:
                pass



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

    @staticmethod
    async def _show_visual_cursor(page: Page, x: int, y: int):
        """在页面上显示一个波纹动画光标，提示用户系统正在此坐标进行视觉操作"""
        try:
            await page.evaluate(f'''
                (() => {{
                    const el = document.createElement('div');
                    el.style.position = 'fixed';
                    el.style.left = ({x} - 15) + 'px';
                    el.style.top = ({y} - 15) + 'px';
                    el.style.width = '30px';
                    el.style.height = '30px';
                    el.style.borderRadius = '50%';
                    el.style.backgroundColor = 'rgba(239, 68, 68, 0.5)';
                    el.style.border = '2px solid rgba(239, 68, 68, 0.8)';
                    el.style.pointerEvents = 'none';
                    el.style.zIndex = '2147483647';
                    el.style.transition = 'all 0.6s cubic-bezier(0.16, 1, 0.3, 1)';
                    document.body.appendChild(el);
                    
                    // 强制引起重排，使初始状态生效
                    void el.offsetWidth;

                    setTimeout(() => {{
                        el.style.transform = 'scale(2.5)';
                        el.style.opacity = '0';
                    }}, 10);
                    
                    setTimeout(() => el.remove(), 700);
                }})()
            ''')
        except Exception:
            pass

    async def _smart_click(self, page: Page, x: int, y: int, selector: str = "") -> bool:
        """
        智能点击：全方位打击死角级难点组件
        """
        await self._show_visual_cursor(page, x, y)

        # 策略1: 如果有 CSS 选择器，优先使用 Playwright 的元素级强制点击
        if selector:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    # 加入 force=True 穿透透明遮罩
                    await locator.first.click(timeout=3000, force=True)
                    print_agent("vision", f"  ✅ 选择器 (强制) 点击成功: {selector}")
                    return True
            except Exception as e:
                logger.debug(f"选择器点击失败 [{selector}]: {e}")

        # 策略2: 平滑仿真移动 + 精修后的物理坐标点击（绕过单纯 Click 验证与 Hover 检查）
        try:
            # 模拟人的鼠标移过去
            await page.mouse.move(x, y, steps=10)
            await asyncio.sleep(0.05)
            await page.mouse.down()
            await asyncio.sleep(0.05)
            await page.mouse.up()
            print_agent("vision", f"  ✅ 物理仿真坐标点击: ({x}, {y})")
            return True
        except Exception as e:
            logger.debug(f"坐标点击失败 ({x},{y}): {e}")

        # 策略3: 用 JS 构建完整的事件栈触发并冒泡
        try:
            clicked = await page.evaluate(f"""
                (() => {{
                    const el = document.elementFromPoint({x}, {y});
                    if (!el) return false;
                    
                    // 构建完全仿真的事件序列来欺骗一些 React / Vue 的底层绑定
                    const eventOpts = {{ bubbles: true, cancelable: true, view: window }};
                    el.dispatchEvent(new MouseEvent('pointerover', eventOpts));
                    el.dispatchEvent(new MouseEvent('pointerenter', eventOpts));
                    el.dispatchEvent(new MouseEvent('mouseover', eventOpts));
                    el.dispatchEvent(new MouseEvent('mousedown', eventOpts));
                    el.dispatchEvent(new MouseEvent('mouseup', eventOpts));
                    el.dispatchEvent(new MouseEvent('click', eventOpts));
                    
                    // 同样尝试直接的原生触发
                    el.click();
                    return true;
                }})()
            """)
            if clicked:
                print_agent("vision", f"  ✅ JS 深度事件流触发布线成功")
                return True
        except Exception as e:
            logger.debug(f"JS 点击失败: {e}")

        return False

    async def _smart_fill(self, page: Page, x: int, y: int, value: str, selector: str = "") -> bool:
        """
        智能填写：优先用 selector 定位输入框
        """
        await self._show_visual_cursor(page, x, y)

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
                raw_x = int(na.get("coordinates", {}).get("x", 0))
                raw_y = int(na.get("coordinates", {}).get("y", 0))
                
                if raw_x > 0 and raw_y > 0:
                    x, y = await self._scale_llm_coords(page, raw_x, raw_y, screenshot_path)
                else:
                    x, y = raw_x, raw_y
                
                return VisionAction(
                    action_type=na.get("action_type", "click"),
                    target_description=na.get("target_description", ""),
                    coordinates={"x": x, "y": y},
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
                raw_x = int(coords.get("x", 0))
                raw_y = int(coords.get("y", 0))
                if raw_x > 0 and raw_y > 0:
                    x, y = await self._scale_llm_coords(page, raw_x, raw_y, screenshot_path)
                else:
                    x, y = raw_x, raw_y
                    
                return (
                    x,
                    y,
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
