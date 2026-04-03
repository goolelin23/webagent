# 🤖 智能Web自动化Agent系统 (WebAgent)

基于 **Browser Use + AI + Playwright** 的六层架构智能体自动化操作系统。

通过自然语言指令，自动完成Web系统的扫描学习和自动化操作。

## ✨ 核心特性

- 🔍 **智能扫描** — 使用 Browser Use 自动探索Web系统，构建知识库
- 📋 **自然语言规划** — 将自然语言指令转化为精确的执行步骤
- ⚡ **精准执行** — 基于 Playwright 的精准页面操作
- 🔄 **自愈机制** — 失败自动重试、弹窗处理、路径重新规划
- 🔧 **技能插件** — 可扩展的技能系统（价格计算、日期处理等）
- 🛡️ **安全治理** — 风险分类、权限管理、审计日志

## 🏗️ 六层架构

```
┌─────────────────────────────────────────┐
│     第一层：入口层 (CLI)                 │
│     支持交互模式 / 单次指令 / 扫描模式    │
├─────────────────────────────────────────┤
│     第二层：提示词引擎                    │
│     动态拼装 + 业务上下文注入             │
├─────────────────────────────────────────┤
│     第三层：Agent 调度                    │
│     探索Agent / 规划Agent / 执行Agent     │
├─────────────────────────────────────────┤
│     第四层：工具管线                      │
│     状态校验 → 安全过滤 → 执行 → 重试     │
├─────────────────────────────────────────┤
│     第五层：生态扩展                      │
│     技能插件系统 (价格计算/日期格式化...)   │
├─────────────────────────────────────────┤
│     第六层：安全治理                      │
│     风险分类器 / 权限管理 / 审计日志       │
└─────────────────────────────────────────┘
```

## 🚀 快速开始

### 1. 环境要求

- Python >= 3.11
- 至少一个 LLM API Key (OpenAI / Anthropic / Google)

### 2. 安装

```bash
# 克隆项目
cd webagent

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -e .

# 安装 Playwright 浏览器
playwright install chromium
```

### 3. 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，配置你的 API Key
# OPENAI_API_KEY=your-api-key
```

### 4. 使用

#### 交互模式
```bash
python -m webagent
# 或
webagent
```

#### 执行指令
```bash
webagent run "按照最新流程创建一个采购订单" --url https://your-system.com
```

#### 扫描系统
```bash
webagent scan --url https://your-system.com --depth 3
```

#### 查看知识库
```bash
webagent kb list
webagent kb show your-system.com
```

#### 查看技能
```bash
webagent skills
```

## 📋 交互模式命令

| 命令 | 说明 |
|------|------|
| `<指令>` | 直接输入自然语言指令 |
| `/scan <url>` | 扫描Web系统 |
| `/plan <指令>` | 仅生成执行计划 |
| `/kb list` | 查看知识库列表 |
| `/domain <name>` | 设置业务领域 |
| `/url <url>` | 设置目标URL |
| `/skills` | 查看技能插件 |
| `/quit` | 退出 |

## 🔧 业务领域

系统预置了三个业务领域模板：

- **supply_chain** — 供应链管理（采购订单、收货、库存）
- **hr** — 人力资源管理（入职、考勤、薪资）
- **ecommerce** — 电子商务（商品管理、订单处理）

```bash
webagent run "创建采购订单" --domain supply_chain --url https://erp.example.com
```

## 🔌 技能插件开发

创建自定义技能插件：

```python
# src/webagent/skills/builtin/my_skill.py

from webagent.skills.base_skill import BaseSkill, SkillResult

class MyCustomSkill(BaseSkill):
    name = "my_skill"
    description = "我的自定义技能"
    version = "1.0.0"

    async def execute(self, params: dict) -> SkillResult:
        result = params.get("input", "") + " processed"
        return SkillResult(success=True, value=result)
```

放入 `src/webagent/skills/builtin/` 目录，系统会自动发现并注册。

## 🛡️ 安全级别

在 `.env` 中设置安全级别：

| 级别 | 说明 |
|------|------|
| `low` | 仅拦截 CRITICAL 操作 |
| `medium` | 拦截 HIGH + CRITICAL（默认） |
| `high` | 拦截 MEDIUM + HIGH + CRITICAL |

涉及**删除数据、修改系统配置、批量操作**等高风险行为会被自动拦截并要求人工审批。

## 📁 项目结构

```
webagent/
├── src/webagent/
│   ├── cli/           # 第一层：入口层
│   ├── prompt_engine/ # 第二层：提示词引擎
│   ├── agents/        # 第三层：Agent调度
│   ├── pipeline/      # 第四层：工具管线
│   ├── skills/        # 第五层：生态扩展
│   ├── safety/        # 第六层：安全治理
│   ├── knowledge/     # 知识库
│   └── utils/         # 工具集
├── knowledge_base/    # 知识库数据
├── logs/              # 日志
└── tests/             # 测试
```

## 📜 License

MIT
