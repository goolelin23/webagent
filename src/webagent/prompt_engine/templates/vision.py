"""
视觉模型提示词模板
用于截图理解、操作推理、结果验证
"""

VISION_PERCEIVE_PROMPT = """观察截图，选择一个未探索的元素操作。

目标: {goal}

已探索历史（禁止重复）:
{action_history}

已探索编号（禁止再选）:
{explored_element_ids}

规则: 选截图中编号最小的未探索可交互元素。全部已探索则设is_dead_end=true。不要点装饰性元素。

只返回JSON:
{{
    "page_description": "页面简述(10字)",
    "next_action": {{
        "action_type": "click|fill|scroll_down|scroll_up|hover|select",
        "element_id": "数字编号",
        "target_description": "元素描述(位置+文字)",
        "coordinates": {{"x": X坐标, "y": Y坐标}},
        "value": "fill时的值,否则空",
        "reasoning": "选择理由(15字内)",
        "risk_level": "safe|dangerous"
    }},
    "is_dead_end": false,
    "dead_end_reason": ""
}}
"""

VISION_VERIFY_PROMPT = """对比前后两张截图，判断操作是否成功。
操作: {action_description}

只返回JSON:
{{
    "success": true或false,
    "page_changed": true或false,
    "change_description": "变化描述(20字内)",
    "error_detected": false,
    "error_message": ""
}}
"""

VISION_LOCATE_PROMPT = """在截图中找到指定元素的中心坐标。
元素: {element_description}

只返回JSON:
{{
    "found": true或false,
    "coordinates": {{"x": X坐标, "y": Y坐标}},
    "confidence": 0.0到1.0
}}
"""

VISION_ZOOM_REFINE_PROMPT = """这是局部放大截图。在图中精确定位目标元素中心。
元素: {element_description}

只返回JSON:
{{
    "found": true或false,
    "coordinates": {{"x": 本图X像素, "y": 本图Y像素}},
    "confidence": 0.0到1.0
}}
"""

VISION_SOM_FALLBACK_PROMPT = """截图已标注红色数字标签(SOM)。找出漏标的可交互元素(导航栏、工具栏等)。

只返回JSON:
{{
    "missing_elements": [
        {{"description": "元素描述", "x_percent": 0.15, "y_percent": 0.05}}
    ]
}}
"""
