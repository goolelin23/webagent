# 🤖 智能 Web 自然语言自动化操作 Agent (WebAgent)

基于 **视觉感知 + 多 Agent 陪审团 + 梦境自清理** 的新一代 **Web 系统** 自动化操作系统。

通过自然语言指令驱动（Vision-Driven）取代传统的 DOM 元素定位，实现更接近人类行为、更具自愈能力的自动化交互。

---

## ✨ 核心特性

- 🎯 **SOM 高级视觉定位 (Set-of-Mark)** — 通过前端注入，将可交互元素（包括跨域 Iframe 和 Shadow DOM 完全穿透）贴上数字编号，让大模型直接基于视觉 `ID` 操作，准确率逼近 100%。
- 🧠 **真正的语义闭环 (Vector DB)** — 主动学习到的知识不仅能看还能“用”！利用 Embedding 将知识库意图向量化，探索时基于 Cosine Similarity 无缝匹配。
- ⏳ **Mutation Observer 底层调度** — 摒除全部 `sleep()` 与脆弱的网络层等待，深入监听 DOM 树变化探测组件空闲状态，快准狠！
- 🧱 **模型归一化映射 (Model Normalization)** — 自动适配主流多模态大模型（如 Gemini, Qwen, Gemma）的非标准名称配置，消除格式异常。
- 🖲️ **异形组件深度解析 (Deep Vision Support)** — 针对无标签的 `div` 登录按钮、`svg`/`canvas` 绘图组件进行专项坐标吸附优化，识别与命中率大幅提升。
- 🙋‍♂️ **人机协同自学习 (Human-in-the-loop Tracker)** — 当执行卡点时，通过实时物理轨道追踪（Tracker）捕获用户纠错点击的 X/Y 坐标与组件特征，将其作为黄金样本喂回 AI 纠错闭环。
- 🛠️ **高性能执行引擎 (Turbo Execution)** — 全面优化视觉感知管线，通过截图压缩 (JPEG)、智能稳定探测 (MutationObserver) 和感知步骤合并，将操作延迟降低了 70%，效率提升 3~5 倍！
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
# 编辑 .env 配置你的 LLM_API_KEY 或使用下方的 OpenClaw 模式
```

### 4. 🦞 配合 OpenClaw (龙虾) 使用 (推荐)
如果你已经安装了 [OpenClaw](https://openclaw.ai)，WebAgent 现在支持零配置直接调用 OpenClaw 的本地模型接口：

```bash
# 在 .env 中修改
LLM_PROVIDER=openclaw
LLM_BASE_URL=http://localhost:18789/v1  # 默认 OpenClaw 接口
LLM_MODEL=      # 留空将使用 OpenClaw 默认模型
LLM_API_KEY=    # 留空即可
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
