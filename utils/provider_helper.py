"""从 AstrBot 提供商加载 LLM API 配置的共享工具。"""

from dataclasses import dataclass

from astrbot.core.provider.entities import ProviderType

DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"


@dataclass
class LLMApiConfig:
    api_key: str = ""
    model: str = ""
    api_base: str = ""
    is_gemini: bool = False

    @property
    def valid(self) -> bool:
        return bool(self.api_key)


def load_mood_provider(context, settings) -> LLMApiConfig:
    """从情绪分析提供商读取 API 参数。

    优先使用 settings.mood_provider_id 指定的提供商，
    找不到则回退到默认 CHAT_COMPLETION 提供商。
    """
    cfg = LLMApiConfig()

    provider_mgr = getattr(context, "provider_manager", None)
    if not provider_mgr or not hasattr(provider_mgr, "inst_map"):
        return cfg

    provider = None
    if settings.mood_provider_id:
        provider = provider_mgr.inst_map.get(settings.mood_provider_id)
    if not provider:
        provider = provider_mgr.get_using_provider(
            ProviderType.CHAT_COMPLETION, None
        )
    if not provider:
        return cfg

    # API key
    keys = provider.get_keys() or []
    if keys:
        cfg.api_key = str(keys[0]).strip()

    # Model
    cfg.model = (
        provider.get_model()
        or provider.provider_config.get("model_config", {}).get("model")
        or "gemini-2.0-flash-exp"
    )

    # 判断提供商类型
    prov_type = provider.provider_config.get("type", "")
    cfg.is_gemini = "google" in prov_type.lower() or "gemini" in prov_type.lower()

    # API base
    prov_base = provider.provider_config.get("api_base", "").rstrip("/")
    if cfg.is_gemini:
        if prov_base and prov_base.endswith("/v1"):
            prov_base = prov_base.removesuffix("/v1")
        cfg.api_base = (prov_base or DEFAULT_GEMINI_BASE).rstrip("/")
    else:
        cfg.api_base = (prov_base or DEFAULT_OPENAI_BASE).rstrip("/")

    return cfg
