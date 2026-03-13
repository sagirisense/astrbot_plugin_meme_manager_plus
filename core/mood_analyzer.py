import traceback

import aiohttp

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderType

from ..config.settings import DEFAULT_MOOD_PROMPT

DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class MoodAnalyzer:
    """通过直接调用 LLM API 分析文本情绪。

    绕过 provider.text_chat 避免在 on_llm_response 钩子中死锁。
    """

    def __init__(self, context, settings):
        self.context = context
        self.settings = settings
        self._api_key: str = ""
        self._model: str = ""
        self._api_base: str = ""
        self._is_gemini: bool = False

    def _load_api_config(self) -> bool:
        """从情绪分析提供商读取 API 参数。"""
        s = self.settings
        provider_mgr = getattr(self.context, "provider_manager", None)
        if not provider_mgr or not hasattr(provider_mgr, "inst_map"):
            return False

        provider = None
        if s.mood_provider_id:
            provider = provider_mgr.inst_map.get(s.mood_provider_id)
        if not provider:
            provider = provider_mgr.get_using_provider(
                ProviderType.CHAT_COMPLETION, None
            )
        if not provider:
            return False

        keys = provider.get_keys() or []
        if keys:
            self._api_key = str(keys[0]).strip()

        self._model = (
            provider.get_model()
            or provider.provider_config.get("model_config", {}).get("model")
            or "gemini-2.0-flash-exp"
        )

        # 判断提供商类型
        prov_type = provider.provider_config.get("type", "")
        self._is_gemini = "google" in prov_type.lower() or "gemini" in prov_type.lower()

        prov_base = provider.provider_config.get("api_base", "")
        if prov_base:
            prov_base = prov_base.rstrip("/")
        if self._is_gemini:
            if prov_base and prov_base.endswith("/v1"):
                prov_base = prov_base.removesuffix("/v1")
            self._api_base = (prov_base or DEFAULT_GEMINI_BASE).rstrip("/")
        else:
            self._api_base = (prov_base or "https://api.openai.com/v1").rstrip("/")

        return bool(self._api_key)

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

        if not self._load_api_config():
            logger.warning("[MemeMemPlus] 未找到情绪分析 API 配置")
            return 0.0, None

        categories_str = ", ".join(available_moods)
        prompt_template = self.settings.custom_mood_prompt or DEFAULT_MOOD_PROMPT
        prompt = prompt_template.replace("{categories}", categories_str).replace("{text}", text[:500])
        system_msg = (
            "You are a mood and expression desire analyzer. "
            "Output ONLY in the format: score|mood (e.g. 0.85|happy). Nothing else."
        )

        logger.debug(f"[MemeMemPlus] 情绪分析请求: model={self._model}, gemini={self._is_gemini}")

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if self._is_gemini:
                    result_text = await self._call_gemini(session, prompt, system_msg)
                else:
                    result_text = await self._call_openai(session, prompt, system_msg)

                if not result_text:
                    return 0.0, None

                return self._parse_result(result_text, available_moods)

        except aiohttp.ClientError as e:
            logger.error(f"[MemeMemPlus] 情绪分析网络错误: {e}")
            return 0.0, None
        except Exception:
            logger.error(f"[MemeMemPlus] 情绪分析异常: {traceback.format_exc()}")
            return 0.0, None

    async def _call_gemini(self, session: aiohttp.ClientSession, prompt: str, system_msg: str) -> str | None:
        """Gemini API 调用。"""
        api_base = self._api_base
        if not api_base.endswith(("/v1beta", "/v1")):
            url = f"{api_base}/v1beta/models/{self._model}:generateContent"
        else:
            url = f"{api_base}/models/{self._model}:generateContent"

        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 50},
            "systemInstruction": {"parts": [{"text": system_msg}]},
        }

        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"[MemeMemPlus] Gemini API 错误 {resp.status}: {error_text[:200]}")
                return None
            data = await resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            for part in candidates[0].get("content", {}).get("parts", []):
                if "text" in part:
                    return part["text"].strip()
        return None

    async def _call_openai(self, session: aiohttp.ClientSession, prompt: str, system_msg: str) -> str | None:
        """OpenAI 兼容 API 调用。"""
        api_base = self._api_base
        if not api_base.endswith("/v1"):
            url = f"{api_base}/v1/chat/completions"
        else:
            url = f"{api_base}/chat/completions"

        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 50,
        }

        logger.debug(f"[MemeMemPlus] OpenAI 请求: url={url}, model={self._model}")

        async with session.post(url, headers=headers, json=payload) as resp:
            logger.debug(f"[MemeMemPlus] OpenAI 响应: status={resp.status}")
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"[MemeMemPlus] OpenAI API 错误 {resp.status}: {error_text[:200]}")
                return None
            data = await resp.json()
            choices = data.get("choices", [])
            if not choices:
                logger.debug("[MemeMemPlus] OpenAI 响应无 choices")
                return None
            msg = choices[0].get("message", {})
            # 优先取 content，为空时取 reasoning_content（deepseek-reasoner 等推理模型）
            content = msg.get("content", "").strip()
            if not content:
                content = msg.get("reasoning_content", "").strip()
            # 推理模型可能输出很长，只取最后一行
            if content and "\n" in content:
                content = content.strip().split("\n")[-1].strip()
            logger.debug(f"[MemeMemPlus] OpenAI 返回内容: '{content}'")
            return content
        return None

    def _parse_result(
        self, result: str, available_moods: list[str]
    ) -> tuple[float, str | None]:
        """解析 LLM 返回的 score|mood 格式。"""
        result = result.strip()
        score = 0.5  # 默认中等表达欲望

        if "|" in result:
            parts = result.split("|", 1)
            try:
                score = float(parts[0].strip())
                score = max(0.0, min(1.0, score))  # clamp to [0, 1]
            except ValueError:
                pass
            mood_raw = parts[1].strip().lower()
        else:
            mood_raw = result.strip().lower()
            mood_raw = mood_raw.split()[0] if mood_raw else ""

        mood_raw = mood_raw.strip(".,!?;:\"'()[]{}*")

        for mood in available_moods:
            if mood.lower() == mood_raw:
                logger.debug(f"[MemeMemPlus] 情绪分析结果: score={score:.2f}, mood={mood}")
                return score, mood

        logger.debug(f"[MemeMemPlus] 情绪分析结果 '{mood_raw}' 不在可用列表中 (score={score:.2f})")
        return score, None
