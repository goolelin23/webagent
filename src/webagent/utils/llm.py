"""
LLM 工厂模块
根据配置创建不同提供商的聊天模型实例
"""

from langchain_core.language_models import BaseChatModel

from webagent.utils.config import get_config


_llm_instance: BaseChatModel | None = None


def get_llm() -> BaseChatModel:
    """
    根据配置创建 LLM 实例（单例缓存）

    支持的 LLM_PROVIDER 值:
      - openai     → ChatOpenAI (GPT-4o, GPT-4 等)
      - anthropic  → ChatAnthropic (Claude 系列)
      - gemini     → ChatGoogleGenerativeAI (Gemini 系列)
      - qwen       → ChatQwen (千问系列)
    """
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    config = get_config()
    provider = config.llm.provider.lower()

    if not config.llm.api_key:
        raise ValueError(
            f"未配置 {provider} 的 API Key。\n"
            f"请在 .env 文件中设置对应的 API Key，参考 .env.example"
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        kwargs = {
            "model": config.llm.model,
            "api_key": config.llm.api_key,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
        }
        kwargs["base_url"] = "https://api.openai.com/v1"
        _llm_instance = ChatOpenAI(**kwargs)

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        _llm_instance = ChatAnthropic(
            model=config.llm.model,
            api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        _llm_instance = ChatGoogleGenerativeAI(
            model=config.llm.model,
            google_api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            max_output_tokens=config.llm.max_tokens,
        )

    elif provider == "qwen":
        from langchain_qwq import ChatQwen
        _llm_instance = ChatQwen(
            model=config.llm.model,
            api_key=config.llm.api_key,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )

    else:
        raise ValueError(
            f"不支持的 LLM 提供商: {provider}\n"
            f"支持的提供商: openai, anthropic, gemini, qwen"
        )

    return _llm_instance


def get_provider_info() -> str:
    """获取当前 LLM 提供商信息（用于 CLI 展示）"""
    config = get_config()
    provider = config.llm.provider
    model = config.llm.model
    has_key = "✅" if config.llm.api_key else "❌"
    return f"{provider} / {model} {has_key}"


def get_embeddings():
    """获取文本嵌入模型用于语义检索库"""
    config = get_config()
    try:
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(openai_api_key=config.llm.api_key, openai_api_base="https://api.openai.com/v1")
    except Exception as e:
        import logging
        logging.warning(f"无法初始化Embedding模型: {e}。检索将回退无向量模式。")
        return None
