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
    sticker_mode: bool = False

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
        s.max_library_size = self._get("generation_settings", "max_library_size", default=0)
        # Sticker
        s.sticker_mode = self._get("generation_settings", "sticker_mode", default=False)

        # Cooldown
        s.cooldown_seconds = self._get("cooldown_settings", "cooldown_seconds", default=60)
        s.per_group = self._get("cooldown_settings", "per_group", default=True)

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

        return s
