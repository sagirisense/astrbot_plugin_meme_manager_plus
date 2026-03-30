import random
import traceback

from astrbot.api import logger

from ..config.settings import DEFAULT_MOOD_PROMPT
from ..utils.provider_helper import load_mood_provider
from ..utils.llm_client import LLMClient


class MoodAnalyzer:
    """通过直接调用 LLM API 分析文本情绪。

    绕过 provider.text_chat 避免在 on_llm_response 钩子中死锁。
    """

    def __init__(self, context, settings):
        self.context = context
        self.settings = settings

    async def analyze(
        self, text: str, available_moods: list[str]
    ) -> tuple[float, str | None]:
        """分析文本情绪和表达欲望。

        Returns:
            (score, mood) 元组。score 为表达欲望 0.0-1.0，mood 为情绪关键词。
            失败时返回 (0.0, None)。
        """
        if not text or not text.strip():
            return 0.0, None
        if not available_moods:
            return 0.0, None

        cfg = load_mood_provider(self.context, self.settings)
        if not cfg.valid:
            logger.warning("[MemeMemPlus] 未找到情绪分析 API 配置")
            return 0.0, None

        categories_str = ", ".join(available_moods)
        prompt_template = self.settings.custom_mood_prompt or DEFAULT_MOOD_PROMPT
        prompt = prompt_template.replace("{categories}", categories_str).replace("{text}", text[:500])
        system_msg = (
            "You are a mood classifier. You MUST respond with exactly one line: score|mood. "
            "score is a float 0.0-1.0, mood is one word from the given list. "
            "Example: 0.85|happy. Do NOT output anything else — no explanation, no number alone, no extra text."
        )

        logger.info(f"[MemeMemPlus] 情绪分析请求: model={cfg.model}, gemini={cfg.is_gemini}")

        try:
            result_text = await LLMClient.call(
                cfg, prompt,
                system_msg=system_msg,
                max_tokens=50,
                timeout=self.settings.llm_timeout,
                single_line=True,
            )
            if not result_text:
                logger.warning("[MemeMemPlus] 情绪分析 API 返回为空")
                return 0.0, None

            logger.info(f"[MemeMemPlus] LLM 原始返回: '{result_text}'")
            return self._parse_result(result_text, available_moods)

        except Exception:
            logger.error(f"[MemeMemPlus] 情绪分析异常: {traceback.format_exc()}")
            return 0.0, None

    def _parse_result(
        self, result: str, available_moods: list[str]
    ) -> tuple[float, str | None]:
        """解析 LLM 返回的 score|mood 格式，带模糊匹配回退。"""
        result = result.strip()
        score = 0.5  # 默认中等表达欲望
        mood_raw = ""

        if "|" in result:
            parts = result.split("|", 1)
            try:
                score = float(parts[0].strip())
                score = max(0.0, min(1.0, score))
            except ValueError:
                pass
            mood_raw = parts[1].strip().lower()
        else:
            mood_raw = result.strip().lower()

        mood_raw = mood_raw.strip(".,!?;:\"'()[]{}* \t\n")

        # 1) 精确匹配
        for mood in available_moods:
            if mood.lower() == mood_raw:
                logger.info(f"[MemeMemPlus] 情绪分析结果: score={score:.2f}, mood={mood}")
                return score, mood

        # 2) 模糊匹配：在整个返回文本中搜索心情关键词
        result_lower = result.lower()
        for mood in available_moods:
            if mood.lower() in result_lower:
                logger.info(f"[MemeMemPlus] 情绪分析结果(模糊匹配): score={score:.2f}, mood={mood}")
                return score, mood

        # 3) 数字回退：LLM 可能返回了索引号
        try:
            idx = int(mood_raw)
            if 0 <= idx < len(available_moods):
                mood = available_moods[idx]
                logger.info(f"[MemeMemPlus] 情绪分析结果(索引回退): score={score:.2f}, mood={mood}, raw='{mood_raw}'")
                return score, mood
        except (ValueError, IndexError):
            pass

        # 4) 全部失败，随机选一个，score=0 确保不会误触发
        fallback = random.choice(available_moods)
        logger.warning(f"[MemeMemPlus] 情绪分析无法解析 '{result}', 随机回退: mood={fallback}")
        return 0.0, fallback
