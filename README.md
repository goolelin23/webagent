# 🤖 智能 Web 自然语言自动化操作 Agent (WebPilot AI)
# 🤖 WebPilot AI: Intelligent Web Natural Language Automation Agent

基于 **视觉感知 + 多 Agent 陪审团 + 梦境自清理** 的新一代 **Web 系统** 自动化操作系统。
A next-generation **Web System** automation OS based on **Vision Perception + Multi-Agent Jury + Dream Self-Cleaning**.

通过网页自然语言指令驱动（Vision-Driven）取代传统的 DOM 元素定位，实现更接近人类行为、更具自愈能力的自动化交互。
Driving interactions via web natural language instructions (Vision-Driven) instead of fragile DOM locators, achieving human-like and self-healing automation.

---

## ✨ 核心特性 / Core Features

- 🎯 **SOM 高级视觉定位 (Set-of-Mark)** — 通过前端注入，将可交互元素（包括跨域 Iframe 和 Shadow DOM 完全穿透）贴上数字编号，让大模型直接基于视觉 `ID` 操作，准确率逼近 100%。
  **Advanced Visual Positioning** — Injects visual IDs (SOM) to interactive elements, allowing LLMs to target elements via visual IDs with near 100% accuracy, even through cross-domain Iframes and Shadow DOM.
- 🧠 **真正的语义闭环 (Vector DB)** — 主动学习到的知识不仅能看还能“用”！利用 Embedding 将知识库意图向量化，探索时基于 Cosine Similarity 无缝匹配。
  **Semantic Loop-closure** — Learned knowledge is vectorized using embeddings, enabling seamless intent matching via Cosine Similarity during exploration.
- ⏳ **Mutation Observer 底层调度** — 摒除全部 `sleep()` 与脆弱的网络层等待，深入监听 DOM 树变化探测组件空闲状态，快准狠！
  **Mutation Observer Scheduling** — Replaces fragile `sleep()` with deep DOM monitoring to detect component idle states, ensuring speed and reliability.
- 🧱 **模型归一化映射 (Model Normalization)** — 自动适配主流多模态大模型（如 Gemini, Qwen, Gemma）的非标准名称配置，消除格式异常。
  **Model Normalization** — Automatically maps non-standard model identifiers (e.g., Gemini, Qwen, Gemma) to official API endpoints.
- 🖲️ **异形组件深度解析 (Deep Vision Support)** — 针对无标签的 `div` 登录按钮、`svg`/`canvas` 绘图组件进行专项坐标吸附优化，识别与命中率大幅提升。
  **Deep Vision Support** — Optimized coordinate snapping for unlabeled `div` buttons, `svg`, and `canvas` components, significantly improving recognition rates.
- 🙋‍♂️ **人机协同自学习 (Human-in-the-loop Tracker)** — 当执行卡点时，通过实时物理轨道追踪（Tracker）捕获用户纠错点击的 X/Y 坐标与组件特征，将其作为黄金样本喂回 AI 纠错闭环。
  **Human-in-the-loop Tracker** — When automation is stuck, a real-time tracker captures your manual corrections (coordinates and features) as "golden samples" for self-learning.
- 🛠️ **高性能执行引擎 (Turbo Execution)** — 全面优化视觉感知管线，通过截图压缩 (JPEG)、智能稳定探测 (MutationObserver) 和感知步骤合并，将操作延迟降低了 70%，效率提升 3~5 倍！
  **Turbo Execution Engine** — Optimized vision pipeline with JPEG compression and step merging, reducing latency by 70%.
- 🛡️ **沙盒防卫审计 (Safety Intercept)** — 风险动态拦截，一旦大模型意识到动作有破坏/写表单倾向，立马封禁死胡同路径，保障生产环境不被探索污染。
  **Safety Intercept** — Dynamically blocks risky actions (e.g., deletions) to prevent production environments from being polluted by AI exploration.

---

## 🏗️ 核心架构 / Core Architecture

```
┌───────────────────────────────────────────┐
│        入口层 / Entry Layer (CLI)          │
│      交互 / 扫描 / 梦境整理 / 回放         │
│     Chat / Scan / Dream / Replay          │
├───────────────────────────────────────────┤
│        决策与评审层 / Jury Panel           │
│     操作执行 → 视觉验证 → 陪审团评分       │
│     Action → Validation → Jury Review     │
├───────────────────────────────────────────┤
│        视觉感知探测层 / Vision SOM         │
│     截图打标 → 大模型看图选号 → 动作隔离   │
│     Marking → Vision Selection → Isolation │
├───────────────────────────────────────────┤
│        自进化知识库 / Vector DB            │
│     向量语义提取 → 余弦匹配(>0.85)可复用   │
│     Vectorization → Semantic Matching     │
├───────────────────────────────────────────┤
│        管线与安全 / Pipeline & Safety      │
│     自愈回退 → 风险过滤 → 审计日志         │
│     Recovery → Risk Filtering → Audit     │
└───────────────────────────────────────────┘
```

---

## 🚀 快速开始 / Quick Start

### 1. 安装 uv (推荐) / Install uv (Recommended)
`uv` 是目前最快的 Python 包管理器。 / `uv` is the fastest Python package manager.

- **Mac/Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows**: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

### 2. 初始化项目 / Initialize Project
```bash
# 克隆项目 / Clone repository
git clone https://github.com/goolelin23/webpilot.git
cd webpilot

# 自动同步环境 / Sync environment
uv sync

# 安装浏览器驱动 / Install browser drivers
uv run playwright install chromium
```

### 3. 配置 / Configuration
```bash
cp .env.example .env
# 编辑 .env 配置你的 LLM_API_KEY / Edit .env with your API Key
```

### 4. 🦞 配合 OpenClaw 使用 / Use with OpenClaw
WebPilot AI 支持零配置直接调用 [OpenClaw](https://openclaw.ai) 的本地模型接口：
WebPilot AI supports zero-config integration with OpenClaw local models:

```bash
# 在 .env 中修改 / Modify in .env
LLM_PROVIDER=openclaw
LLM_BASE_URL=http://localhost:18789/v1
```

---

## 📋 常用指令集 / Command Reference

### 视觉驱动探索 / Vision-driven Exploration
```bash
# 启动视觉驱动深度扫描 / Start deep vision scan
uv run webpilot scan --url https://example.com --deep

# 进入交互式机长模式 / Enter interactive Pilot mode
uv run webpilot
```

### 知识管理与优化 / Knowledge Management
```bash
# 💤 启动梦境模式：整理知识库 / Start Dream Mode (Self-cleaning)
uv run webpilot /dream example.com

# 🔄 回放已学习的操作路径 / Replay learned paths
uv run webpilot /replay example.com

# 查看所有已学到的操作 / List all learned actions
uv run webpilot kb list
```

---

## ⚖️ 陪审团制度 / Jury System

每一个被记录的操作都必须通过评审： / Every recorded action must pass review:
*   **探索价值 (Discovery)**：是否发现了新页面？ / New pages discovered?
*   **业务价值 (Business)**：是否推进了真实流程？ / Advanced real business workflows?
*   **质量评分 (Quality)**：坐标是否精准？ / Precise coordinates & fallback?

---

## 📜 许可证 / License

本项目基于 MIT 协议开源。 / This project is licensed under the MIT License.
