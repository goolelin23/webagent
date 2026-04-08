"""
统一配置管理模块
从 .env 文件和环境变量加载系统配置
支持 OpenAI / Anthropic (Claude) / Google (Gemini) / 千问 (Qwen) 等多模型提供商
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv


# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _load_env():
    """加载环境变量，优先级：系统环境变量 > .env 文件"""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # 尝试从 .env.example 复制
        example_path = PROJECT_ROOT / ".env.example"
        if example_path.exists():
            load_dotenv(example_path)


_load_env()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 支持的 LLM 提供商注册表
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROVIDER_DEFAULTS = {
    # provider → (default_model, api_base_env_var, default_api_base)
    "openai":    ("gpt-4o",          "OPENAI_API_BASE", None),
    "anthropic": ("claude-sonnet-4-20250514", None, None),
    "gemini":    ("gemini-2.0-flash", None, None),
    "qwen":      ("qwen-max",        "QWEN_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


@dataclass
class LLMConfig:
    """LLM 模型配置 — 统一管理多模型提供商"""
    provider: str = os.getenv("LLM_PROVIDER", "openai")
    model: str = os.getenv("LLM_MODEL", "")  # 空则使用提供商默认值

    # 统一 API Key 配置（配合 LLM_PROVIDER 使用）
    api_key: str = os.getenv("LLM_API_KEY", "")

    # 自定义 API Base URL（千问和私有部署需要）
    openai_api_base: str = os.getenv("OPENAI_API_BASE", "")
    qwen_api_base: str = os.getenv(
        "QWEN_API_BASE",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    # 通用参数
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))

    @property
    def effective_model(self) -> str:
        """获取实际使用的模型名称（用户指定 > 提供商默认）"""
        if self.model:
            return self.model
        provider_info = PROVIDER_DEFAULTS.get(self.provider)
        if provider_info:
            return provider_info[0]
        return "gpt-4o"

    @property
    def effective_api_key(self) -> str:
        """获取 API Key"""
        return self.api_key

    @property
    def effective_api_base(self) -> str | None:
        """获取当前提供商对应的 API Base URL"""
        provider_info = PROVIDER_DEFAULTS.get(self.provider)
        if not provider_info:
            return None
        base_env_var = provider_info[1]
        default_base = provider_info[2]
        if base_env_var:
            env_value = os.getenv(base_env_var, "")
            if env_value:
                return env_value
        return default_base


@dataclass
class BrowserConfig:
    """浏览器配置"""
    headless: bool = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    viewport_width: int = int(os.getenv("BROWSER_VIEWPORT_WIDTH", "1280"))
    viewport_height: int = int(os.getenv("BROWSER_VIEWPORT_HEIGHT", "800"))
    browser_use_api_key: str = os.getenv("BROWSER_USE_API_KEY", "")
    use_cloud: bool = os.getenv("BROWSER_USE_CLOUD", "false").lower() == "true"


@dataclass
class SafetyConfig:
    """安全配置"""
    level: str = os.getenv("SAFETY_LEVEL", "medium")  # low, medium, high
    max_actions_per_minute: int = int(os.getenv("MAX_ACTIONS_PER_MINUTE", "30"))
    blocked_url_patterns: list[str] = field(default_factory=lambda: [
        "*/admin/delete*",
        "*/system/config*",
    ])


@dataclass
class PipelineConfig:
    """管线配置"""
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    retry_delay: float = float(os.getenv("RETRY_DELAY", "2.0"))
    page_load_timeout: int = int(os.getenv("PAGE_LOAD_TIMEOUT", "30000"))
    element_timeout: int = int(os.getenv("ELEMENT_TIMEOUT", "10000"))


@dataclass
class AppConfig:
    """应用总配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/webagent.log")
    knowledge_base_dir: str = os.getenv("KNOWLEDGE_BASE_DIR", "knowledge_base")

    @property
    def knowledge_base_path(self) -> Path:
        """知识库绝对路径"""
        p = Path(self.knowledge_base_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_file_path(self) -> Path:
        """日志文件绝对路径"""
        p = Path(self.log_file)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def get_config() -> AppConfig:
    """获取全局配置单例"""
    return AppConfig()


def get_llm():
    """
    根据配置创建 LLM 实例（统一工厂方法）

    支持的 LLM_PROVIDER 值:
      - openai     → ChatOpenAI (GPT-4o, GPT-4 等)
      - anthropic  → ChatAnthropic (Claude 系列)
      - gemini     → ChatGoogleGenerativeAI (Gemini 系列)
      - qwen       → ChatOpenAI + DashScope 兼容端点 (千问系列)
    """
    config = get_config()
    provider = config.llm.provider.lower()
    model = config.llm.effective_model
    api_key = config.llm.effective_api_key

    if not api_key:
        raise ValueError(
            f"未配置 {provider} 的 API Key。\n"
            f"请在 .env 文件中设置对应的 API Key，参考 .env.example"
        )

    # ── OpenAI ──
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        kwargs = {
            "model": model,
            "api_key": api_key,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
        }
        base_url = config.llm.effective_api_base
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    # ── Anthropic (Claude) ──
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )

    # ── Google (Gemini) ──
    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=config.llm.temperature,
            max_output_tokens=config.llm.max_tokens,
        )

    # ── 千问 (Qwen) — 通过 DashScope OpenAI 兼容接口 ──
    elif provider == "qwen":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=config.llm.effective_api_base,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )

    else:
        supported = ", ".join(PROVIDER_DEFAULTS.keys())
        raise ValueError(
            f"不支持的 LLM 提供商: {provider}\n"
            f"支持的提供商: {supported}"
        )


def get_provider_info() -> str:
    """获取当前 LLM 提供商信息（用于 CLI 展示）"""
    config = get_config()
    provider = config.llm.provider
    model = config.llm.effective_model
    has_key = "✅" if config.llm.effective_api_key else "❌"
    return f"{provider} / {model} {has_key}"


def get_embeddings():
    """获取文本嵌入模型用于语义检索库"""
    config = get_config()
    provider = config.llm.provider
    
    try:
        from langchain_openai import OpenAIEmbeddings
        # Defaulting to OpenAI Embeddings assuming compatible base_url or OpenAI itself
        api_key = config.llm.api_key
        base_url = config.llm.effective_api_base
        return OpenAIEmbeddings(openai_api_key=api_key, openai_api_base=base_url)
    except Exception as e:
        import logging
        logging.warning(f"无法初始化Embedding模型: {e}。检索将回退无向量模式。")
        return None
