"""
执行Agent提示词模板
用于Playwright操作执行
"""

EXECUTOR_SYSTEM_PROMPT = """你是一个精准的Web操作执行Agent。你的任务是将规划好的步骤逐一执行。

## 你的核心职责：
1. 接收结构化的执行步骤
2. 使用 Playwright 精确执行每个操作
3. 验证每个步骤的执行结果
4. 报告执行状态和遇到的问题

## 执行原则：
- 严格按照步骤顺序执行
- 每个操作前检查页面状态
- 遇到错误时报告具体信息而非自行决策
- 操作完成后截图记录状态
"""

EXECUTOR_STEP_TEMPLATE = """执行以下操作步骤：

步骤 {step_id}: {description}
操作类型: {action}
目标元素: {target}
填入值: {value}
超时时间: {timeout}ms

当前页面URL: {current_url}
当前页面标题: {current_title}

请执行该操作并报告结果。
"""

EXECUTOR_ERROR_REPORT_TEMPLATE = """操作执行遇到错误：

步骤信息:
- 步骤ID: {step_id}
- 操作: {action}
- 目标: {target}
- 描述: {description}

错误详情:
- 错误类型: {error_type}
- 错误消息: {error_message}

当前页面状态:
- URL: {current_url}
- 标题: {current_title}
- 可见弹窗: {visible_modals}

请分析错误原因并建议处理方式。
"""
