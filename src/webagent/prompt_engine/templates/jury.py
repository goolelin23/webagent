"""
陪审团评审提示词模板
三位评审员从不同维度评分，控制为单次 LLM 调用
"""

JURY_REVIEW_PROMPT = """你是一个由三位专家组成的评审团，正在对一个Web自动化智能体学到的操作进行质量评审。

## 操作信息
- 操作类型: {action_type}
- 操作描述: {action_description}
- 目标坐标: {coordinates}
- CSS选择器: {selector_hint}
- 页面URL: {page_url}
- 页面变化: {page_change}

## 操作上下文
- 当前探索目标: {exploration_goal}
- 已学习的操作数量: {learned_count}
- 当前操作在本页第几步: {step_number}

请你分别以三位评审员的视角打分（0-10分）并给出理由：

### 评审员1 — 探索价值评审
评判标准：这个操作是否带来了有意义的新发现？
- 页面是否切换到了新页面？
- 是否展开了之前看不到的新内容/新菜单？
- 是否只是重复了已有的操作路径？
- 点击装饰性、不产生变化的元素应得低分

### 评审员2 — 业务价值评审
评判标准：这个操作是否有业务意义？
- 是否是系统核心功能的入口（菜单、导航、工具栏）？
- 是否推进了某个业务流程（如 填表→提交→查看结果）？
- 无意义的操作（如反复滚动、点击 logo、点击装饰图片）应得低分

### 评审员3 — 技术质量评审
评判标准：这个操作的技术可靠性如何？
- 是否有 CSS 选择器可供精准定位？（有则加分）
- 坐标是否在合理的可视区域内？
- 操作是否可复现（描述是否清晰明确）？

请返回 JSON（且仅返回 JSON）：
{{
    "explorer_review": {{
        "score": 0到10的整数,
        "reasoning": "探索价值评价（一句话）"
    }},
    "business_review": {{
        "score": 0到10的整数,
        "reasoning": "业务价值评价（一句话）"
    }},
    "quality_review": {{
        "score": 0到10的整数,
        "reasoning": "技术质量评价（一句话）"
    }},
    "final_verdict": {{
        "average_score": 三位评审的平均分（保留一位小数）,
        "approved": true或false（平均分>=6为true）,
        "summary": "一句话总结评审结论"
    }}
}}
"""

DREAM_SUMMARIZE_PROMPT = """你是一个知识库整理专家。请对以下Web系统的已学习操作进行业务语义分析和总结。

## 站点信息
- 域名: {domain}
- 站点名称: {site_name}

## 已学习的操作列表
{actions_text}

请分析这些操作，返回 JSON：
{{
    "business_flows": [
        {{
            "flow_name": "业务流程名称（如: 用户管理流程）",
            "description": "流程描述",
            "involved_actions": ["操作ID1", "操作ID2"],
            "importance": "high/medium/low"
        }}
    ],
    "redundant_groups": [
        {{
            "description": "这组操作为何重复",
            "action_ids": ["重复的操作ID1", "重复的操作ID2"],
            "keep_id": "应该保留的操作ID"
        }}
    ],
    "low_value_actions": ["应删除的低价值操作ID列表"],
    "knowledge_summary": "对这个系统已学习知识的整体总结（2-3句话）"
}}
"""
