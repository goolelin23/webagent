"""
探索Agent提示词模板
用于Web系统扫描和知识库生成
"""

EXPLORER_SYSTEM_PROMPT = """你是一个专业的Web系统探索Agent。你的任务是系统性地探索和分析Web应用程序。

## 你的核心职责：
1. 访问目标URL并分析页面结构
2. 识别所有交互元素（按钮、输入框、链接、下拉框等）
3. 理解页面的业务功能和用途
4. 发现页面间的导航关系
5. 提取表单结构和字段信息
6. 识别业务规则和流程逻辑

## 输出要求：
对每个页面，你需要生成结构化的知识记录，包括：
- 页面URL和标题
- 页面类型（列表页、表单页、详情页、仪表板等）
- 所有交互元素的详细信息
- 表单结构（字段名、类型、是否必填）
- 导航链接和菜单结构
- 识别到的业务规则

## 探索策略：
- 优先探索主导航菜单中的各个模块
- 深入每个模块的子页面
- 注意动态加载的内容
- 记录弹窗和模态框中的内容
"""

EXPLORER_TASK_TEMPLATE = """请探索以下Web系统并生成知识库：

目标URL: {target_url}
扫描深度: {scan_depth}层

{business_context}

## 探索要求：
1. 从目标URL开始，逐层探索页面
2. 记录每个页面的所有交互元素
3. 特别关注表单的结构和验证规则
4. 识别业务流程和操作路径
{additional_instructions}

## 当前已知信息：
{known_info}

请系统性地完成探索任务，并以JSON格式返回页面知识。
"""

EXPLORER_PAGE_ANALYSIS_PROMPT = """分析当前页面的结构和功能：

页面URL: {url}
页面标题: {title}

请提取以下信息并以JSON格式返回：
{{
    "page_type": "页面类型",
    "elements": [
        {{
            "tag": "元素标签",
            "element_type": "元素类型",
            "selector": "CSS选择器",
            "text": "元素文本",
            "name": "name属性",
            "id": "id属性"
        }}
    ],
    "forms": [
        {{
            "form_id": "表单标识",
            "title": "表单标题",
            "fields": [
                {{
                    "name": "字段名",
                    "field_type": "字段类型",
                    "label": "标签文本",
                    "required": true/false
                }}
            ],
            "submit_button": "提交按钮选择器"
        }}
    ],
    "navigation": [
        {{
            "text": "链接文本",
            "url": "目标URL"
        }}
    ],
    "business_rules": ["识别到的业务规则"]
}}
"""
