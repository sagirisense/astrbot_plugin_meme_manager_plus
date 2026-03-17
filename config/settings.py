from dataclasses import dataclass


DEFAULT_MOOD_PROMPT = (
    "You must analyze the text below and respond with EXACTLY one line in this format:\n"
    "score|mood\n\n"
    "Rules:\n"
    "- score: a decimal number between 0.0 and 1.0 representing expression desire\n"
    "  (0.0-0.3 = neutral/factual, 0.3-0.7 = mild emotion, 0.7-1.0 = strong emotion)\n"
    "- mood: MUST be one of these exact words: {categories}\n"
    "- Use | as separator, no spaces around it\n\n"
    "CORRECT examples: 0.85|happy  0.20|neutral  0.95|excited\n"
    "WRONG examples: happy  0  1  0.5  good  I think it's happy\n\n"
    "Text to analyze:\n{text}\n\n"
    "Your response (ONLY score|mood, nothing else):"
)

DEFAULT_IMAGE_PROMPT = (
    "Generate a high-quality expressive emoji/sticker reaction image. "
    "The character should clearly convey the emotion: {mood}. "
    "Style: detailed, clean lines, vibrant colors, suitable for chat sticker. "
    "Square format, white or transparent background. "
    "Ensure high detail on facial features."
)

DEFAULT_REFERENCE_ADDON = (
    "CRITICAL REQUIREMENTS for image generation:\n"
    "1. You are given reference images of a specific character. Study them VERY carefully.\n"
    "2. Generate a NEW image of THIS EXACT SAME CHARACTER - DO NOT change the art style, "
    "DO NOT change the character design, DO NOT create a different character.\n"
    "3. Keep EVERYTHING identical to the reference: face shape, eye shape, eye color, "
    "hair style, hair color, hair accessories, clothing, body proportions, line style, "
    "coloring style, shading style, background style.\n"
    "4. The ONLY thing you should change is the FACIAL EXPRESSION to match the mood: {mood}.\n"
    "5. Think of it as drawing the next frame of the same character - NOT a new character.\n"
    "6. If the reference is anime style, output anime style. If chibi, output chibi. "
    "Match the exact art style precisely."
)


@dataclass
class PluginSettings:
    enabled: bool = True

    # Gemini API
    provider_id: str = ""
    image_provider_type: str = "gemini"  # "gemini" or "grok"
    model: str = ""
    timeout: int = 60
    resolution: str = "1K"
    aspect_ratio: str = "1:1"

    # Mood analysis
    mood_provider_id: str = ""
    custom_mood_prompt: str = ""
    llm_timeout: int = 30

    # Probability
    expression_threshold: float = 0.65
    llm_generation_enabled: bool = True
    llm_generation_probability: int = 30

    # Generation
    image_prompt_template: str = DEFAULT_IMAGE_PROMPT
    reference_prompt_addon: str = DEFAULT_REFERENCE_ADDON
    max_library_size: int = 0  # 0=不限制

    # Sticker
    sticker_mode: bool = True

    # Cooldown
    cooldown_seconds: int = 60
    per_group: bool = True

    # Auto update
    auto_update_enabled: bool = False
    auto_update_interval_hours: float = 6.0
    auto_update_search_tags: str = "eris_greyrat solo"
    auto_update_images_per_cycle: int = 5
    auto_update_source: str = "danbooru"
    auto_update_min_score: int = 10
    auto_update_filter_prompt: str = ""
    pixiv_refresh_token: str = ""
    pixiv_search_keyword: str = ""
    pixiv_search_target: str = "partial_match_for_tags"
    pixiv_allow_r18: bool = False

    # NovelAI 独立模式
    novelai_enabled: bool = False
    novelai_api_key: str = ""
    novelai_model: str = "nai-diffusion-4-5-full"
    novelai_base_tags: str = "1girl, solo"
    novelai_negative_prompt: str = "lowres, {bad}, error, missing, extra, fewer, cropped, worst quality, bad quality, watermark, text, signature, jpeg artifacts, blurry, flat color"
    novelai_probability: int = 30
    novelai_width: int = 832
    novelai_height: int = 1216
    novelai_steps: int = 28
    novelai_scale: float = 5.0
    novelai_sampler: str = "k_euler_ancestral"
    novelai_tag_prompt: str = ""
    novelai_r18: bool = False  # R18 模式：LLM 额外生成衣着/身体/行为标签
    novelai_safe_mode: bool = True  # 安全模式：非 R18 时强制 SFW 约束
    novelai_use_outfit: bool = False  # 从 life_scheduler 获取今日穿搭注入标签
    novelai_custom_tags: str = ""  # 用户自定义标签，直接追加到最终正向标签末尾
    novelai_r18_custom_tags: str = ""  # R18 模式专用自定义标签
    novelai_llm_enabled: bool = True  # 是否用 LLM 补全标签，关闭后直接用 base_tags
    novelai_sticker_mode: bool = True  # NovelAI 独立小图模式
    novelai_direct_model: str = ""  # /ni 原图模式专用模型，留空使用 novelai_model
    novelai_tag_history_size: int = 0  # 标签历史缓存条数，0=关闭
    novelai_history_weight: float = 0.8  # 标签历史最新条目权重（最旧=0.3，线性衰减到此值）
    novelai_outfit_weight: float = 0.85  # 穿搭 tag 权重
    novelai_cooldown_seconds: int = 60
    novelai_max_cache: int = 100  # novelai/ 目录最大图片数，0=不限制
    # 高级生图参数
    novelai_seed: int = -1  # -1=随机
    novelai_quality_toggle: bool = True
    novelai_uc_preset: int = 0  # 0=Heavy, 1=Light, 2=Human Focus, 3=None
    novelai_cfg_rescale: float = 0.0
    novelai_noise_schedule: str = "karras"
    novelai_dynamic_thresholding: bool = False  # Decrisper
    novelai_smea: bool = False  # V3 SMEA
    novelai_smea_dyn: bool = False  # V3 Dynamic SMEA
    novelai_variety_boost: float = 0.0  # skip_cfg_above_sigma, 0=关闭
    # 参考图
    novelai_use_reference: bool = False
    novelai_reference_mode: str = "vibe_transfer"  # "vibe_transfer", "img2img", "director"
    novelai_reference_strength: float = 0.6
    novelai_reference_info_extracted: float = 1.0
    novelai_img2img_strength: float = 0.6
    novelai_img2img_noise: float = 0.0
    novelai_director_strength: float = 0.5
    novelai_director_fidelity: float = 0.5
    novelai_director_info_extracted: float = 1.0


class ConfigLoader:
    def __init__(self, raw_config):
        self._raw = raw_config

    def _get(self, *keys, default=None):
        """Nested config access: _get("gemini_api_settings", "timeout", default=60)"""
        val = self._raw
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def load(self) -> PluginSettings:
        s = PluginSettings()

        s.enabled = self._get("enabled", default=True)

        # Gemini API settings
        s.provider_id = self._get("gemini_api_settings", "provider_id", default="")
        s.image_provider_type = self._get("gemini_api_settings", "image_provider_type", default="gemini")
        s.model = self._get("gemini_api_settings", "model", default="")
        s.timeout = self._get("gemini_api_settings", "timeout", default=60)
        s.resolution = self._get("gemini_api_settings", "resolution", default="1K")
        s.aspect_ratio = self._get("gemini_api_settings", "aspect_ratio", default="1:1")

        # Mood analysis
        s.mood_provider_id = self._get("mood_analysis_settings", "mood_provider_id", default="")
        s.custom_mood_prompt = self._get("mood_analysis_settings", "custom_mood_prompt", default="")
        s.llm_timeout = self._get("mood_analysis_settings", "llm_timeout", default=30)

        # Probability
        s.expression_threshold = self._get("probability_settings", "expression_threshold", default=0.65)
        s.llm_generation_enabled = self._get("probability_settings", "llm_generation_enabled", default=True)
        s.llm_generation_probability = self._get("probability_settings", "llm_generation_probability", default=30)

        # Generation
        s.image_prompt_template = self._get(
            "generation_settings", "image_prompt_template", default=DEFAULT_IMAGE_PROMPT
        )
        s.reference_prompt_addon = self._get(
            "generation_settings", "reference_prompt_addon", default=DEFAULT_REFERENCE_ADDON
        )
        _max_lib = self._get("library_settings", "max_library_size", default=None)
        s.max_library_size = _max_lib if _max_lib is not None else self._get("generation_settings", "max_library_size", default=0)
        # Sticker
        s.sticker_mode = self._get("generation_settings", "sticker_mode", default=False)

        # Cooldown (新位置: library_settings，兼容旧位置: cooldown_settings)
        _cd = self._get("library_settings", "cooldown_seconds", default=None)
        s.cooldown_seconds = _cd if _cd is not None else self._get("cooldown_settings", "cooldown_seconds", default=60)
        per_group = self._get("library_settings", "per_group", default=None)
        s.per_group = per_group if per_group is not None else self._get("cooldown_settings", "per_group", default=True)

        # Auto update
        s.auto_update_enabled = self._get("auto_update_settings", "auto_update_enabled", default=False)
        s.auto_update_interval_hours = self._get("auto_update_settings", "auto_update_interval_hours", default=6.0)
        s.auto_update_search_tags = self._get("auto_update_settings", "auto_update_search_tags", default="eris_greyrat solo")
        s.auto_update_images_per_cycle = self._get("auto_update_settings", "auto_update_images_per_cycle", default=5)
        s.auto_update_source = self._get("auto_update_settings", "auto_update_source", default="danbooru")
        s.auto_update_min_score = self._get("auto_update_settings", "auto_update_min_score", default=10)
        s.auto_update_filter_prompt = self._get("auto_update_settings", "auto_update_filter_prompt", default="")
        s.pixiv_refresh_token = self._get("auto_update_settings", "pixiv_refresh_token", default="")
        s.pixiv_search_keyword = self._get("auto_update_settings", "pixiv_search_keyword", default="")
        s.pixiv_search_target = self._get("auto_update_settings", "pixiv_search_target", default="partial_match_for_tags")
        s.pixiv_allow_r18 = self._get("auto_update_settings", "pixiv_allow_r18", default=False)

        # NovelAI
        s.novelai_enabled = self._get("novelai_settings", "novelai_enabled", default=False)
        s.novelai_api_key = self._get("novelai_settings", "novelai_api_key", default="")
        s.novelai_model = self._get("novelai_settings", "novelai_model", default="nai-diffusion-4-5-full")
        s.novelai_base_tags = self._get("novelai_settings", "novelai_base_tags", default="1girl, solo")
        s.novelai_negative_prompt = self._get("novelai_settings", "novelai_negative_prompt", default="lowres, {bad}, error, missing, extra, fewer, cropped, worst quality, bad quality, watermark, text, signature, jpeg artifacts, blurry, flat color")
        s.novelai_probability = self._get("novelai_settings", "novelai_probability", default=30)
        s.novelai_width = self._get("novelai_settings", "novelai_width", default=832)
        s.novelai_height = self._get("novelai_settings", "novelai_height", default=1216)
        s.novelai_steps = self._get("novelai_settings", "novelai_steps", default=28)
        s.novelai_scale = self._get("novelai_settings", "novelai_scale", default=5.0)
        s.novelai_sampler = self._get("novelai_settings", "novelai_sampler", default="k_euler_ancestral")
        s.novelai_tag_prompt = self._get("novelai_settings", "novelai_tag_prompt", default="")
        s.novelai_r18 = self._get("novelai_settings", "novelai_r18", default=False)
        s.novelai_safe_mode = self._get("novelai_settings", "novelai_safe_mode", default=True)
        s.novelai_use_outfit = self._get("novelai_settings", "novelai_use_outfit", default=False)
        s.novelai_custom_tags = self._get("novelai_settings", "novelai_custom_tags", default="")
        s.novelai_r18_custom_tags = self._get("novelai_settings", "novelai_r18_custom_tags", default="")
        s.novelai_llm_enabled = self._get("novelai_settings", "novelai_llm_enabled", default=True)
        s.novelai_sticker_mode = self._get("novelai_settings", "novelai_sticker_mode", default=True)
        s.novelai_direct_model = self._get("novelai_settings", "novelai_direct_model", default="")
        s.novelai_tag_history_size = self._get("novelai_settings", "novelai_tag_history_size", default=0)
        s.novelai_history_weight = self._get("novelai_settings", "novelai_history_weight", default=0.8)
        s.novelai_outfit_weight = self._get("novelai_settings", "novelai_outfit_weight", default=0.85)
        s.novelai_cooldown_seconds = self._get("novelai_settings", "novelai_cooldown_seconds", default=60)
        s.novelai_max_cache = self._get("novelai_settings", "novelai_max_cache", default=100)
        # 高级生图参数
        s.novelai_seed = self._get("novelai_settings", "novelai_seed", default=-1)
        s.novelai_quality_toggle = self._get("novelai_settings", "novelai_quality_toggle", default=True)
        s.novelai_uc_preset = self._get("novelai_settings", "novelai_uc_preset", default=0)
        s.novelai_cfg_rescale = self._get("novelai_settings", "novelai_cfg_rescale", default=0.0)
        s.novelai_noise_schedule = self._get("novelai_settings", "novelai_noise_schedule", default="karras")
        s.novelai_dynamic_thresholding = self._get("novelai_settings", "novelai_dynamic_thresholding", default=False)
        s.novelai_smea = self._get("novelai_settings", "novelai_smea", default=False)
        s.novelai_smea_dyn = self._get("novelai_settings", "novelai_smea_dyn", default=False)
        s.novelai_variety_boost = self._get("novelai_settings", "novelai_variety_boost", default=0.0)
        # 参考图
        s.novelai_use_reference = self._get("novelai_settings", "novelai_use_reference", default=False)
        s.novelai_reference_mode = self._get("novelai_settings", "novelai_reference_mode", default="vibe_transfer")
        s.novelai_reference_strength = self._get("novelai_settings", "novelai_reference_strength", default=0.6)
        s.novelai_reference_info_extracted = self._get("novelai_settings", "novelai_reference_info_extracted", default=1.0)
        s.novelai_img2img_strength = self._get("novelai_settings", "novelai_img2img_strength", default=0.6)
        s.novelai_img2img_noise = self._get("novelai_settings", "novelai_img2img_noise", default=0.0)
        s.novelai_director_strength = self._get("novelai_settings", "novelai_director_strength", default=0.5)
        s.novelai_director_fidelity = self._get("novelai_settings", "novelai_director_fidelity", default=0.5)
        s.novelai_director_info_extracted = self._get("novelai_settings", "novelai_director_info_extracted", default=1.0)

        # 校验关键数值范围
        s.expression_threshold = max(0.0, min(1.0, s.expression_threshold))
        s.llm_generation_probability = max(0, min(100, s.llm_generation_probability))
        s.cooldown_seconds = max(0, s.cooldown_seconds)
        s.novelai_probability = max(0, min(100, s.novelai_probability))
        s.novelai_cooldown_seconds = max(0, s.novelai_cooldown_seconds)
        s.novelai_scale = max(0.0, s.novelai_scale)
        s.novelai_steps = max(1, min(50, s.novelai_steps))
        s.novelai_width = max(64, s.novelai_width)
        s.novelai_height = max(64, s.novelai_height)
        s.max_library_size = max(0, s.max_library_size)
        s.novelai_max_cache = max(0, s.novelai_max_cache)
        s.novelai_tag_history_size = max(0, min(20, s.novelai_tag_history_size))
        s.novelai_history_weight = max(0.1, min(1.5, s.novelai_history_weight))
        s.novelai_outfit_weight = max(0.1, min(1.5, s.novelai_outfit_weight))
        # 参考图参数 0-1 范围
        s.novelai_reference_strength = max(0.0, min(1.0, s.novelai_reference_strength))
        s.novelai_reference_info_extracted = max(0.0, min(1.0, s.novelai_reference_info_extracted))
        s.novelai_img2img_strength = max(0.0, min(1.0, s.novelai_img2img_strength))
        s.novelai_img2img_noise = max(0.0, min(1.0, s.novelai_img2img_noise))
        s.novelai_director_strength = max(0.0, min(1.0, s.novelai_director_strength))
        s.novelai_director_fidelity = max(0.0, min(1.0, s.novelai_director_fidelity))
        s.novelai_director_info_extracted = max(0.0, min(1.0, s.novelai_director_info_extracted))
        s.novelai_cfg_rescale = max(0.0, min(1.0, s.novelai_cfg_rescale))
        # 枚举值校验
        if s.novelai_reference_mode not in ("vibe_transfer", "img2img", "director"):
            s.novelai_reference_mode = "vibe_transfer"
        if s.image_provider_type.lower() not in ("gemini", "grok"):
            s.image_provider_type = "gemini"

        return s
