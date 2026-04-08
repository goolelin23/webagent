"""
视觉感知引擎
通过截图 + 多模态大语言模型理解页面，取代传统的DOM解析
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
)
from webagent.utils.logger import get_logger, print_agent
from webagent.utils.config import get_config, get_llm

logger = get_logger("webagent.agents.vision_engine")


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

    核心循环: 截图 → 多模态LLM推理 → 返回结构化操作
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

    async def _screenshot(self, page: Page, label: str = "") -> str:
        """截图并返回文件路径"""
        ts = int(time.time() * 1000)
        filename = f"{label}_{ts}.png" if label else f"screenshot_{ts}.png"
        path = self.screenshots_dir / filename
        await page.screenshot(path=str(path), full_page=False)
        return str(path)

    def _image_to_base64(self, image_path: str) -> str:
        """将截图转为 base64"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    async def _call_vision_llm(self, prompt: str, image_paths: list[str]) -> str:
        """
        调用多模态 LLM（支持 Claude / GPT-4o / Gemini 的图片输入）
        """
        from langchain_core.messages import HumanMessage

        llm = self._get_llm()

        # 构建包含图片的消息
        content = []
        for img_path in image_paths:
            b64 = self._image_to_base64(img_path)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}",
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
        # 尝试 ```json 代码块
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 直接尝试解析
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                return json.loads(text[json_start:json_end])
            except json.JSONDecodeError:
                pass
        return None

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

        Args:
            page: Playwright 页面
            goal: 当前探索目标（如 "探索系统所有功能"）
            action_history: 已执行的操作历史
        Returns:
            VisionAction 下一步操作
        """
        screenshot_path = await self._screenshot(page, "perceive")
        print_agent("vision", f"📸 截图: {screenshot_path}")

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
                )
        except Exception as e:
            logger.warning(f"视觉感知失败: {e}")

        # 降级：返回死胡同
        return VisionAction(
            action_type="none",
            target_description="",
            coordinates={"x": 0, "y": 0},
            is_dead_end=True,
            dead_end_reason=f"视觉感知异常: {e}" if 'e' in dir() else "未知错误",
        )

    async def verify(
        self,
        page: Page,
        screenshot_before: str,
        action_description: str,
    ) -> tuple[VerifyResult, str]:
        """
        验证：对比操作前后截图，判断操作是否成功

        Args:
            page: Playwright 页面
            screenshot_before: 操作前截图路径
            action_description: 执行的操作描述
        Returns:
            (VerifyResult, screenshot_after_path)
        """
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

        # 降级：假设失败
        return VerifyResult(
            success=False,
            change_description="视觉验证异常",
        ), screenshot_after

    async def locate_element(
        self,
        page: Page,
        element_description: str,
    ) -> tuple[int, int, float]:
        """
        视觉定位：找到指定元素的坐标

        Args:
            page: Playwright 页面
            element_description: 元素的自然语言描述
        Returns:
            (x, y, confidence)
        """
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
        执行视觉操作（基于坐标）

        Args:
            page: Playwright 页面
            action: 视觉模型推理出的操作
        Returns:
            是否执行成功（不含验证）
        """
        x = action.coordinates.get("x", 0)
        y = action.coordinates.get("y", 0)
        action_type = action.action_type

        try:
            if action_type == "click":
                await page.mouse.click(x, y)
                await asyncio.sleep(0.5)

            elif action_type == "double_click":
                await page.mouse.dblclick(x, y)
                await asyncio.sleep(0.5)

            elif action_type == "right_click":
                await page.mouse.click(x, y, button="right")
                await asyncio.sleep(0.5)

            elif action_type == "hover":
                await page.mouse.move(x, y)
                await asyncio.sleep(0.3)

            elif action_type == "fill":
                # 先点击输入框，再输入
                await page.mouse.click(x, y)
                await asyncio.sleep(0.3)
                # 先选中全部再输入（覆盖）
                await page.keyboard.press("Control+a")
                await page.keyboard.type(action.value, delay=30)
                await asyncio.sleep(0.3)

            elif action_type == "select":
                # 对下拉框：先点击展开
                await page.mouse.click(x, y)
                await asyncio.sleep(0.8)
                # 如果有 value，尝试用键盘输入搜索/选择
                if action.value:
                    await page.keyboard.type(action.value, delay=50)
                    await asyncio.sleep(0.3)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.3)

            elif action_type == "scroll_down":
                await page.mouse.wheel(0, 300)
                await asyncio.sleep(0.5)

            elif action_type == "scroll_up":
                await page.mouse.wheel(0, -300)
                await asyncio.sleep(0.5)

            elif action_type == "none":
                return False

            else:
                logger.warning(f"未知的视觉操作类型: {action_type}")
                return False

            return True

        except Exception as e:
            logger.warning(f"视觉操作执行失败: {e}")
            return False
