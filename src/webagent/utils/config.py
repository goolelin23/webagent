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


@dataclass
class LLMConfig:
    """LLM 模型配置 — 统一管理多模型提供商"""
    provider: str = os.getenv("LLM_PROVIDER", "openai")
    model: str = os.getenv("LLM_MODEL", "")  
    api_key: str = os.getenv("LLM_API_KEY", "")
    base_url: str = os.getenv("LLM_BASE_URL", "")

    # 通用参数
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))


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
    max_retries: int = int(os.getenv("MAX_RETRIES", "5"))
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
    log_file: str = os.getenv("LOG_FILE", "logs/webpilot.log")
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


_config_instance: AppConfig | None = None


def get_config() -> AppConfig:
    """获取全局配置单例（缓存）"""
    global _config_instance
    if _config_instance is None:
        _config_instance = AppConfig()
    return _config_instance
