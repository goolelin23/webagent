# 🤖 智能Web自动化Agent系统 (WebAgent) v0.6.0

基于 **视觉感知 + 多Agent陪审团 + 梦境自清理** 的新一代智能Web自动化操作系统。

通过截图理解（Vision-Driven）取代传统的 DOM 元素定位，实现更接近人类行为、更具自愈能力的自动化交互。

---

## ✨ 核心特性

- 🎯 **SOM 高级视觉定位 (Set-of-Mark)** — 通过前端注入，将可交互元素（包括跨域 Iframe 和 Shadow DOM 完全穿透）贴上数字编号，让大模型直接基于视觉 `ID` 操作，准确率逼近 100%。
- 🧠 **真正的语义闭环 (Vector DB)** — 主动学习到的知识不仅能看还能“用”！利用 Embedding 将知识库意图向量化，探索时基于 Cosine Similarity 无缝匹配。
- ⏳ **Mutation Observer 底层调度** — 摒除全部 `sleep()` 与脆弱的网络层等待，深入监听 DOM 树变化探测组件空闲状态，快准狠！
- ⚖️ **多Agent陪审团 (Jury Panel)** — 每一个成功的操作都会经过"探索、业务、质量"三个维度的 AI 交叉评审，杜绝无效沉淀。
- 💤 **梦境自清理 (Dream Mode)** — 知识库会自动"睡眠整理"，合并重复操作、清除低分垃圾、通过 LLM 升华业务总结。
- ⚡ **uv 高性能驱动** — 全面迁移至 `uv` 工具链，提供毫秒级依赖同步与零冲突的环境隔离。
- 🛡️ **沙盒防卫审计 (Safety Intercept)** — 风险动态拦截，一旦大模型意识到动作有破坏/写表单倾向，立马封禁死胡同路径，保障生产环境不被探索污染。

---

## 🏗️ 核心架构 (V6)

```
┌───────────────────────────────────────────┐
│        入口层 (CLI / uv run)               │
│      交互 / 扫描 / 梦境整理 / 回放         │
├───────────────────────────────────────────┤
│        决策与评审层 (Jury Panel)           │
│     操作执行 → 视觉验证 → 陪审团评分       │
├───────────────────────────────────────────┤
│        视觉感知探测层 (Vision SOM)         │
│     截图打标 → 大模型看图选号 → 动作隔离   │
├───────────────────────────────────────────┤
│        自进化知识库 (Vector DB)            │
│     向量语义提取 → 余弦匹配(>0.85)可复用   │
├───────────────────────────────────────────┤
│        管线与安全 (Pipeline & Safety)      │
│     自愈回退 → 风险过滤 → 审计日志         │
└───────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 1. 安装 uv (推荐)
`uv` 是目前最快的 Python 包管理器，它能确保环境永不冲突。

- **Mac/Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows**: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

### 2. 初始化项目
```bash
# 克隆项目
git clone https://github.com/goolelin23/webagent.git
cd webagent

# 自动同步环境 (含 Python 3.11 及所有依赖)
uv sync

# 安装浏览器驱动
uv run playwright install chromium
```

### 3. 配置
```bash
cp .env.example .env
# 编辑 .env 配置你的 LLM_API_KEY
```

---

## 📋 常用指令集

### 视觉驱动探索
```bash
# 启动视觉驱动深度扫描
uv run webagent scan --url https://example.com --deep

# 进入交互式 Agent (支持自然语言对话)
uv run webagent run
```

### 知识管理与优化
```bash
# 💤 启动梦境模式：整理、去重、清理站点知识库
uv run webagent /dream example.com

# 🔄 回放已学习的操作路径
uv run webagent /replay example.com

# 查看所有已学到的操作
uv run webagent kb list
```

---

## ⚖️ 陪审团制度

每一个被记录到知识库的操作都必须通过平均 6 分以上的评审：
*   **探索价值**：是否发现了新页面或新状态？
*   **业务价值**：是否推进了真实的业务流程（如填表、进入菜单）？
*   **质量评分**：坐标是否精准，是有已有 CSS 选择器兜底？

---

## 📜 许可证

本项目基于 MIT 协议开源。
