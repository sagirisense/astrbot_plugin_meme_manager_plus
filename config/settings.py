from dataclasses import dataclass


DEFAULT_MOOD_PROMPT = (
    "Analyze the following text. Do TWO things:\n"
    "1. Rate the 'expression desire' (表达欲望) from 0.0 to 1.0 — how strongly this text "
    "conveys emotion or would benefit from an expressive reaction image. "
    "Factual/neutral text = low (0.0-0.3), mildly emotional = medium (0.3-0.7), "
    "strongly emotional/funny/dramatic = high (0.7-1.0).\n"
    "2. Classify the emotional tone into ONE of these categories: {categories}\n\n"
    "Output format: score|mood  (e.g. 0.85|happy)\n"
    "Output ONLY this format, nothing else.\n\n"
    "Text to analyze:\n{text}"
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

DEFAULT_MOODS = [
    "happy", "sad", "angry", "surprised", "shy",
    "confused", "neutral", "sigh", "cute", "excited",
]


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

    # Probability
    default_probability: int = 20

    # Generation
    image_prompt_template: str = DEFAULT_IMAGE_PROMPT
    reference_prompt_addon: str = DEFAULT_REFERENCE_ADDON
    max_images_per_mood: int = 20

    # Cooldown
    cooldown_seconds: int = 60
    per_group: bool = True


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

        # Probability
        s.default_probability = self._get("probability_settings", "default_probability", default=20)

        # Generation
        s.image_prompt_template = self._get(
            "generation_settings", "image_prompt_template", default=DEFAULT_IMAGE_PROMPT
        )
        s.reference_prompt_addon = self._get(
            "generation_settings", "reference_prompt_addon", default=DEFAULT_REFERENCE_ADDON
        )
        s.max_images_per_mood = self._get(
            "generation_settings", "max_images_per_mood", default=20
        )

        # Cooldown
        s.cooldown_seconds = self._get("cooldown_settings", "cooldown_seconds", default=60)
        s.per_group = self._get("cooldown_settings", "per_group", default=True)

        return s
