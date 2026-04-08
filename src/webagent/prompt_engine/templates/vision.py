"""
视觉模型提示词模板
用于截图理解、操作推理、结果验证
"""

VISION_PERCEIVE_PROMPT = """你是一个Web系统自动化探索智能体。你正在通过截图理解当前页面并决定下一步操作。

## 当前目标
{goal}

## 已执行的操作历史
{action_history}

## 要求
请仔细观察这张截图，分析当前页面上所有可交互的元素（按钮、链接、输入框、下拉框、菜单、标签页、工具栏等），然后选择一个最有价值的下一步操作。

返回 JSON（且仅返回 JSON，不要包含任何其他文字）：
{{
    "page_description": "对当前页面的简要描述",
    "visible_elements": [
        {{
            "element": "元素描述（如：顶部导航栏的"用户管理"菜单项）",
            "type": "button | link | input | select | menu | tab | toolbar_item | checkbox | other",
            "approximate_position": {{"x": 像素坐标X, "y": 像素坐标Y}},
            "already_explored": false
        }}
    ],
    "next_action": {{
        "action_type": "click | fill | scroll_down | scroll_up | hover | select | right_click | double_click",
        "target_description": "要操作的元素的自然语言描述",
        "coordinates": {{"x": 目标X坐标, "y": 目标Y坐标}},
        "value": "如果是fill操作，填入的内容；否则为空字符串",
        "reasoning": "为什么选择这个操作（简述）"
    }},
    "is_dead_end": false,
    "dead_end_reason": ""
}}

注意：
1. 坐标以截图像素为准（左上角为原点）
2. 优先选择未探索过的交互元素
3. 如果页面看起来无法继续操作（空白页、错误页），设 is_dead_end 为 true
4. 对于下拉框，先 click 打开，再在展开的选项中选择
5. 不要选择已经在操作历史中出现过的相同操作
"""

VISION_VERIFY_PROMPT = """你是一个Web操作验证智能体。请对比操作前后的两张截图，判断操作是否成功执行。

## 执行的操作
{action_description}

## 判断标准
1. 页面是否发生了预期的变化？
2. 是否有错误提示、报错弹窗？
3. 是否成功导航到了新页面或展开了新内容？

请返回 JSON（且仅返回 JSON）：
{{
    "success": true或false,
    "page_changed": true或false,
    "change_description": "页面发生了什么变化（或者没有变化的原因）",
    "error_detected": false,
    "error_message": "",
    "suggestion": "如果失败了，建议的补救措施（如：需要先关闭弹窗）"
}}
"""

VISION_LOCATE_PROMPT = """你是一个精确的视觉定位智能体。请在以下截图中找到指定元素的中心坐标。

## 要定位的元素
{element_description}

请返回 JSON（且仅返回 JSON）：
{{
    "found": true或false,
    "coordinates": {{"x": 元素中心X坐标, "y": 元素中心Y坐标}},
    "confidence": 0.0到1.0之间的置信度,
    "element_description": "你在截图中看到的该元素的实际样子"
}}

如果没有找到该元素，设 found 为 false，坐标设为 {{"x": 0, "y": 0}}。
"""
