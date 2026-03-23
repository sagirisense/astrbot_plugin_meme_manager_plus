"""统一的 LLM API 调用客户端，支持 Gemini 和 OpenAI 兼容格式。

消除 mood_analyzer / auto_updater 中重复的 API 调用代码。
复用 aiohttp.ClientSession 避免每次请求重建 TCP/TLS 连接。
"""

import asyncio
import base64
import io

import aiohttp
from PIL import Image

from astrbot.api import logger

from .provider_helper import LLMApiConfig


# Gemini 支持的图片 MIME 类型
_GEMINI_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp"}


def _detect_mime(raw: bytes) -> str:
    """根据文件头检测图片 MIME 类型。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if raw[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def _ensure_gemini_compatible(b64_data: str) -> tuple[str, str]:
    """确保图片是 Gemini 支持的格式，不支持的转为 JPEG。返回 (b64, mime)。"""
    raw = base64.b64decode(b64_data)
    mime = _detect_mime(raw)
    if mime in _GEMINI_SUPPORTED_MIME:
        return b64_data, mime
    # 不支持的格式（GIF/BMP 等）→ 转 JPEG
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        # 转换失败，原样返回让 API 报错
        return b64_data, "image/jpeg"


class LLMClient:
    """LLM 文本/Vision 通用调用，自动分发 Gemini / OpenAI。"""

    _session: aiohttp.ClientSession | None = None
    _session_lock: asyncio.Lock | None = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """懒初始化 asyncio.Lock，确保在当前事件循环中创建。"""
        if cls._session_lock is None:
            cls._session_lock = asyncio.Lock()
        return cls._session_lock

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """获取或创建共享的 ClientSession（线程安全）。"""
        if cls._session and not cls._session.closed:
            return cls._session
        async with cls._get_lock():
            # 双重检查：拿到锁后再看一次
            if cls._session is None or cls._session.closed:
                cls._session = aiohttp.ClientSession()
        return cls._session

    @classmethod
    async def close(cls) -> None:
        """关闭共享 session（插件卸载时调用）。"""
        async with cls._get_lock():
            if cls._session and not cls._session.closed:
                await cls._session.close()
            cls._session = None
        cls._session_lock = None

    # ── URL 构建 ──────────────────────────────────────────

    @staticmethod
    def build_gemini_url(api_base: str, model: str) -> str:
        if not api_base.endswith(("/v1beta", "/v1")):
            return f"{api_base}/v1beta/models/{model}:generateContent"
        return f"{api_base}/models/{model}:generateContent"

    @staticmethod
    def build_openai_url(api_base: str) -> str:
        if not api_base.endswith("/v1"):
            return f"{api_base}/v1/chat/completions"
        return f"{api_base}/chat/completions"

    # ── 统一入口 ──────────────────────────────────────────

    @staticmethod
    async def call(
        cfg: LLMApiConfig,
        prompt: str,
        *,
        system_msg: str | None = None,
        b64_image: str | None = None,
        max_tokens: int = 30,
        timeout: int = 30,
        single_line: bool = False,
    ) -> str | None:
        """自动分发到 Gemini 或 OpenAI，返回文本或 None。

        Args:
            single_line: 为 True 时只保留最后一行（用于情绪分析等单行输出场景）。
        """
        if cfg.is_gemini:
            return await LLMClient._call_gemini(
                cfg, prompt,
                system_msg=system_msg, b64_image=b64_image,
                max_tokens=max_tokens, timeout=timeout,
                single_line=single_line,
            )
        return await LLMClient._call_openai(
            cfg, prompt,
            system_msg=system_msg, b64_image=b64_image,
            max_tokens=max_tokens, timeout=timeout,
            single_line=single_line,
        )

    # ── Gemini ────────────────────────────────────────────

    @staticmethod
    async def _call_gemini(
        cfg: LLMApiConfig,
        prompt: str,
        *,
        system_msg: str | None = None,
        b64_image: str | None = None,
        max_tokens: int = 30,
        timeout: int = 30,
        single_line: bool = False,
    ) -> str | None:
        url = LLMClient.build_gemini_url(cfg.api_base, cfg.model)
        headers = {"x-goog-api-key": cfg.api_key, "Content-Type": "application/json"}

        parts: list[dict] = []
        if b64_image:
            b64_safe, mime = await asyncio.to_thread(_ensure_gemini_compatible, b64_image)
            parts.append({"inlineData": {"mimeType": mime, "data": b64_safe}})
        parts.append({"text": prompt})

        payload: dict = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens},
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_NONE"}
                for c in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_CIVIC_INTEGRITY",
                )
            ],
        }
        if system_msg:
            payload["systemInstruction"] = {"parts": [{"text": system_msg}]}

        try:
            tm = aiohttp.ClientTimeout(total=timeout)
            session = await LLMClient.get_session()
            async with session.post(url, headers=headers, json=payload, timeout=tm) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        f"[MemeMemPlus] Gemini API 错误 {resp.status}: {error_text[:200]}"
                    )
                    return None
                data = await resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    logger.warning(
                        f"[MemeMemPlus] Gemini 返回无 candidates: {str(data)[:300]}"
                    )
                    return None
                for part in candidates[0].get("content", {}).get("parts", []):
                    if "text" in part:
                        text = part["text"].strip()
                        if single_line and "\n" in text:
                            # 取最后一个非空行，防止尾部空行导致返回空字符串
                            lines = [l.strip() for l in text.split("\n") if l.strip()]
                            text = lines[-1] if lines else text
                        return text
        except Exception as e:
            logger.error(f"[MemeMemPlus] Gemini 调用失败: {type(e).__name__}: {e}")
        return None

    # ── OpenAI ────────────────────────────────────────────

    @staticmethod
    async def _call_openai(
        cfg: LLMApiConfig,
        prompt: str,
        *,
        system_msg: str | None = None,
        b64_image: str | None = None,
        max_tokens: int = 30,
        timeout: int = 30,
        single_line: bool = False,
    ) -> str | None:
        url = LLMClient.build_openai_url(cfg.api_base)
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}

        # 构建 user message content
        if b64_image:
            user_content: list | str = [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                {"type": "text", "text": prompt},
            ]
        else:
            user_content = prompt

        messages: list[dict] = []
        if system_msg:
            messages.append({"role": "system", "content": system_msg})
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }

        try:
            tm = aiohttp.ClientTimeout(total=timeout)
            session = await LLMClient.get_session()
            async with session.post(url, headers=headers, json=payload, timeout=tm) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        f"[MemeMemPlus] OpenAI API 错误 {resp.status}: {error_text[:200]}"
                    )
                    return None
                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                msg = choices[0].get("message", {})
                content = msg.get("content", "").strip()
                if not content:
                    content = msg.get("reasoning_content", "").strip()
                if single_line and content and "\n" in content:
                    lines = [l.strip() for l in content.split("\n") if l.strip()]
                    content = lines[-1] if lines else content
                return content or None
        except Exception as e:
            logger.error(f"[MemeMemPlus] OpenAI 调用失败: {type(e).__name__}: {e}")
        return None
