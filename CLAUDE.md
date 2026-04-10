# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

智能 Web 网页自然语言自动化操作 Agent 系统 (WebPilot AI) — 基于视觉感知 + 多 Agent 陪审团 + 梦境自清理的 Web 系统自动化操作系统。支持模型归一化映射、异形组件深度解析以及人机协同自学习机制。

## 常用命令

```bash
# 环境同步 (支持 Pillow 压缩与模型归一化)
uv sync

# 安装浏览器
uv run playwright install chromium

# 启动交互式 Agent
uv run webpilot

# 执行自然语言指令
uv run webpilot run "创建采购订单" --url https://example.com

# 视觉驱动深度扫描
uv run webpilot scan --url https://example.com --deep

# 梦境模式整理知识库
uv run webpilot /dream example.com

# 查看知识库
uv run webpilot kb list
uv run webpilot kb show example.com

# 运行测试
uv run pytest
uv run pytest tests/test_pipeline.py -v
```

## 配置

至少配置一个 LLM 提供商的 API Key：

```bash
cp .env.example .env
# 编辑 .env 设置 ANTHROPIC_API_KEY 或其他提供商 Key
```

支持的 LLM 提供商：`openai`, `anthropic`, `gemini`, `qwen`, `openclaw`

## 核心架构 (六层)

```
┌───────────────────────────────────────────┐
│        入口层 (CLI / Commander)            │
│      cli/commander.py - 命令行交互         │
├───────────────────────────────────────────┤
│        决策与评审层 (Jury Panel)           │
│      agents/jury.py - 三维交叉评审         │
├───────────────────────────────────────────┤
│        视觉感知层 (Vision Engine)          │
│      agents/vision_engine.py - SOM 坐标精修 │
├───────────────────────────────────────────┤
│        调度层 (Orchestrator)               │
│      agents/orchestrator.py - Agent 协调    │
├───────────────────────────────────────────┤
│        知识层 (Knowledge + DeepAnalyzer)   │
│      knowledge/ - 向量存储 + 技能生成       │
├───────────────────────────────────────────┤
│        管线与安全 (Pipeline & Safety)      │
│      pipeline/ - 重试/执行 | safety/ - 分类 │
└───────────────────────────────────────────┘
```

## 目录结构

```
src/webpilot/
├── main.py              # 程序入口 (cli_entry)
├── cli/
│   └── commander.py     # CLI 命令解析与交互
├── agents/
│   ├── orchestrator.py  # Agent 调度器 (核心协调)
│   ├── explorer.py      # 探索 Agent (扫描站点)
│   ├── planner.py       # 规划 Agent (生成执行计划)
│   ├── executor.py      # 执行 Agent (操作浏览器)
│   ├── vision_engine.py # 视觉引擎 (SOM+ 坐标精修)
│   ├── jury.py          # 陪审团 (三维评审)
│   ├── dreamer.py       # 梦境整理 Agent
│   └── active_learner.py# 主动学习 (深度交互扫描)
├── knowledge/
│   ├── models.py        # 数据模型定义
│   ├── store.py         # 知识库存储 (JSON)
│   └── deep_analyzer.py # 深度分析器 (技能/工作流生成)
├── prompt_engine/
│   ├── engine.py        # 提示词引擎
│   ├── context.py       # 上下文管理
│   └── templates/       # 各 Agent 提示词模板
├── pipeline/
│   ├── pipeline.py      # 执行管线
│   ├── retry_manager.py # 重试机制
│   └── state_validator.py
├── safety/
│   ├── classifier.py    # 安全风险分类器
│   ├── permissions.py   # 权限规则
│   └── audit.py         # 审计日志
├── skills/
│   ├── skill_manager.py # 技能注册/执行
│   ├── page_skill_generator.py
│   └── builtin/         # 内置技能
└── utils/
    ├── config.py        # 配置加载 (LLM/浏览器/安全)
    └── logger.py        # 日志工具
```

## 关键设计模式

**视觉 SOM 坐标精修** (`vision_engine.py:383-451`)：三层兜底策略
1. `elementFromPoint(x,y)` → 吸附到真实元素中心
2. 沿 DOM 树上溯找可交互祖先
3. 50px 半径内扫描可交互元素

**陪审团评审** (`jury.py:97-182`)：单次 LLM 调用模拟三位评审员
- 探索价值 → 是否发现新页面/状态
- 业务价值 → 是否推进真实业务流程
- 技术质量 → 坐标是否精准/CSS 兜底

**深度分析器** (`deep_analyzer.py:145-198`)：扫描后 LLM 语义理解
- 逐页分析 → 生成 `PageSkillDef`
- 全局分析 → 识别 `WorkflowDef` (跨页面流程)

**安全分类器** (`safety/classifier.py`)：操作前风险拦截
- 关键词匹配 (删除/重置/导出)
- URL 模式匹配 (admin/config 路径)
- 风险等级：`low` / `medium` / `high`

## 数据模型

核心模型定义在 `knowledge/models.py`：
- `SiteKnowledge` / `PageKnowledge` - 站点/页面知识
- `DeepAnalysis` - 深度分析结果 (技能 + 工作流)
- `ExecutionPlan` / `ExecutionStep` - 执行计划
- `PageSkillDef` / `WorkflowDef` - 技能与工作流定义

## 测试

测试位于 `tests/` 目录，使用 pytest + pytest-asyncio：

```bash
# 全量测试
uv run pytest

# 单文件测试
uv run pytest tests/test_pipeline.py -v

# 单类测试
uv run pytest tests/test_pipeline.py::TestRetryManager -v
```
