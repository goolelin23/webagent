English | [简体中文](./README_CN.md)

# 🤖 WebPilot AI: Intelligent Web Natural Language Automation Agent

A next-generation **Web System** automation OS based on **Vision Perception + Multi-Agent Jury + Dream Self-Cleaning**.

Driving interactions via web natural language instructions (Vision-Driven) instead of fragile DOM locators, achieving human-like and self-healing automation.

---

## ✨ Core Features

- 🎯 **Advanced Visual Positioning (Set-of-Mark)** — Injects visual IDs (SOM) to interactive elements, allowing LLMs to target elements via visual IDs with near 100% accuracy, even through cross-domain Iframes and Shadow DOM.
- 🧠 **Semantic Loop-closure (Vector DB)** — Learned knowledge is vectorized using embeddings, enabling seamless intent matching via Cosine Similarity during exploration.
- ⏳ **Mutation Observer Scheduling** — Replaces fragile `sleep()` with deep DOM monitoring to detect component idle states, ensuring speed and reliability.
- 🧱 **Model Normalization** — Automatically maps non-standard model identifiers (e.g., Gemini, Qwen, Gemma) to official API endpoints.
- 🖲️ **Deep Vision Support** — Optimized coordinate snapping for unlabeled `div` buttons, `svg`, and `canvas` components, significantly improving recognition rates.
- 🙋‍♂️ **Human-in-the-loop Tracker** — When automation is stuck, a real-time tracker captures your manual corrections (coordinates and features) as "golden samples" for self-learning.
- 🛠️ **Turbo Execution Engine** — Optimized vision pipeline with JPEG compression and step merging, reducing latency by 70%.
- 🛡️ **Safety Intercept** — Dynamically blocks risky actions (e.g., deletions) to prevent production environments from being polluted by AI exploration.

---

## 🏗️ Core Architecture

```
┌───────────────────────────────────────────┐
│        Entry Layer (CLI)                  │
│      Chat / Scan / Dream / Replay         │
├───────────────────────────────────────────┤
│        Jury Panel                         │
│     Action → Validation → Jury Review     │
├───────────────────────────────────────────┤
│        Vision SOM                         │
│     Marking → Vision Selection → Isolation │
├───────────────────────────────────────────┤
│        Vector DB                          │
│     Vectorization → Semantic Matching     │
├───────────────────────────────────────────┤
│        Pipeline & Safety                  │
│     Recovery → Risk Filtering → Audit     │
└───────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Install uv (Recommended)
`uv` is the fastest Python package manager.

- **Mac/Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows**: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

### 2. Initialize Project
```bash
# Clone repository
git clone https://github.com/goolelin23/webpilot.git
cd webpilot

# Sync environment
uv sync

# Install browser drivers
uv run playwright install chromium
```

### 3. Configuration
```bash
cp .env.example .env
# Edit .env with your API Key
```

### 4. 🦞 Use with OpenClaw
WebPilot AI supports zero-config integration with [OpenClaw](https://openclaw.ai) local models:

```bash
# Modify in .env
LLM_PROVIDER=openclaw
LLM_BASE_URL=http://localhost:18789/v1
```

---

## 📋 Command Reference

### Vision-driven Exploration
```bash
# Start deep vision scan
uv run webpilot scan --url https://example.com --deep

# Enter interactive Pilot mode
uv run webpilot
```

### Knowledge Management
```bash
# 💤 Start Dream Mode (Self-cleaning)
uv run webpilot /dream example.com

# 🔄 Replay learned paths
uv run webpilot /replay example.com

# List all learned actions
uv run webpilot kb list
```

---

## ⚖️ Jury System

Every recorded action must pass review:
*   **Discovery**: New pages discovered?
*   **Business**: Advanced real business workflows?
*   **Quality**: Precise coordinates & fallback?

---

## 📜 License

This project is licensed under the MIT License.
