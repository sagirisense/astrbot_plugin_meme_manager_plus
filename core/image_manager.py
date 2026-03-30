import asyncio
import base64
import io
import random
import traceback
from pathlib import Path

import aiohttp
from PIL import Image

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderType

from ..config.settings import PluginSettings
from ..utils.llm_client import LLMClient
from ..utils.provider_helper import DEFAULT_GEMINI_BASE

# Gemini 支持的 MIME 类型（不含 GIF/BMP）
GEMINI_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# 需要转换为 JPEG 的格式
CONVERT_FORMATS = {".gif", ".bmp"}

DEFAULT_GROK_BASE = "https://api.x.ai/v1"


class MoodImageManager:
    """通过 Gemini 或 Grok API 生成心情表情图片。

    支持两种引擎：
    - gemini: Gemini API（图生图 + 文生图）
    - grok: xAI Grok API（OpenAI 兼容格式，图编辑 + 文生图）
    """

    def __init__(self, settings: PluginSettings, context=None):
        self.settings = settings
        self.context = context
        self._api_key: str = ""
        self._model: str = ""
        self._api_base: str = ""
        self._is_grok: bool = False

    def _load_api_config(self) -> bool:
        """从 AstrBot 提供商加载 API 参数。返回是否成功。"""
        s = self.settings
        self._is_grok = s.image_provider_type.lower() == "grok"

        if not s.provider_id or not self.context:
            return False

        provider_mgr = getattr(self.context, "provider_manager", None)
        if not provider_mgr or not hasattr(provider_mgr, "inst_map"):
            return False

        provider = provider_mgr.inst_map.get(s.provider_id)
        if not provider:
            provider = provider_mgr.get_using_provider(
                ProviderType.CHAT_COMPLETION, None
            )

        if not provider:
            return False

        keys = provider.get_keys() or []
        if keys:
            self._api_key = str(keys[0]).strip()

        if self._is_grok:
            # Grok: 图片模型和文本模型不同，不从供应商读模型名
            self._model = s.model or "grok-imagine-image"
        else:
            self._model = (
                s.model
                or provider.get_model()
                or provider.provider_config.get("model_config", {}).get("model")
                or "gemini-2.0-flash-exp"
            )

        prov_base = provider.provider_config.get("api_base", "")
        if prov_base:
            prov_base = prov_base.rstrip("/")

        if self._is_grok:
            # Grok: OpenAI 兼容，base 应以 /v1 结尾
            if prov_base and not prov_base.endswith("/v1"):
                prov_base = prov_base + "/v1"
            self._api_base = (prov_base or DEFAULT_GROK_BASE).rstrip("/")
        else:
            # Gemini
            if prov_base and prov_base.endswith("/v1"):
                prov_base = prov_base.removesuffix("/v1")
            self._api_base = (prov_base or DEFAULT_GEMINI_BASE).rstrip("/")

        return bool(self._api_key)

    async def generate(self, mood: str, reference_paths: list[Path] | None) -> bytes | None:
        """生成心情表情图片。"""
        if not self._load_api_config():
            logger.warning("[MemeMemPlus] 未配置生图 API key，无法生图")
            return None

        if self._is_grok:
            return await self._generate_grok(mood, reference_paths)
        else:
            return await self._generate_gemini(mood, reference_paths)

    # ── Gemini 生图 ──────────────────────────────────────────────

    async def _generate_gemini(self, mood: str, reference_paths: list[Path] | None) -> bytes | None:
        has_refs = bool(reference_paths)

        prompt = self.settings.image_prompt_template.replace("{mood}", mood)
        if has_refs:
            addon = self.settings.reference_prompt_addon.replace("{mood}", mood)
            prompt += "\n" + addon

        # 参考图放在 text 前面
        parts = []
        if has_refs:
            loaded = 0
            for ref_path in reference_paths:
                if not ref_path.exists():
                    continue
                try:
                    suffix = ref_path.suffix.lower()
                    image_data = ref_path.read_bytes()

                    # GIF/BMP 等格式 Gemini 不支持，转换为 JPEG
                    if suffix in CONVERT_FORMATS:
                        img = Image.open(io.BytesIO(image_data))
                        if img.mode in ("RGBA", "P", "LA"):
                            img = img.convert("RGB")
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=90)
                        image_data = buf.getvalue()
                        mime_type = "image/jpeg"
                    else:
                        mime_type = GEMINI_MIME_MAP.get(suffix)
                        if not mime_type:
                            # 未知扩展名，转换为 JPEG 确保 Gemini 可识别
                            try:
                                img = Image.open(io.BytesIO(image_data))
                                if img.mode in ("RGBA", "P", "LA"):
                                    img = img.convert("RGB")
                                buf = io.BytesIO()
                                img.save(buf, format="JPEG", quality=90)
                                image_data = buf.getvalue()
                            except Exception:
                                logger.warning(f"[MemeMemPlus] 跳过无效图片: {ref_path.name}")
                                continue
                            mime_type = "image/jpeg"

                    b64_data = base64.b64encode(image_data).decode("utf-8")
                    parts.append({
                        "inlineData": {"mimeType": mime_type, "data": b64_data}
                    })
                    loaded += 1
                except Exception:
                    logger.warning(f"[MemeMemPlus] 读取参考图失败: {ref_path.name}")
            if loaded:
                logger.debug(f"[MemeMemPlus] Gemini 图生图: {loaded} 张参考图")
            else:
                logger.debug("[MemeMemPlus] 参考图全部读取失败，降级为文生图")
        else:
            logger.debug(f"[MemeMemPlus] Gemini 文生图: mood={mood}")

        parts.append({"text": prompt})

        url = LLMClient.build_gemini_url(self._api_base, self._model)

        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        image_config = {"imageSize": self.settings.resolution or "1K"}
        if self.settings.aspect_ratio and self.settings.aspect_ratio != "1:1":
            image_config["aspectRatio"] = self.settings.aspect_ratio
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": image_config,
            },
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout)
            session = await LLMClient.get_session()
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[MemeMemPlus] Gemini API 错误 {resp.status}: {error_text[:200]}")
                    return None
                data = await resp.json()
                return self._parse_gemini_response(data)
        except aiohttp.ClientError as e:
            logger.error(f"[MemeMemPlus] Gemini API 网络错误: {e}")
            return None
        except Exception:
            logger.error(f"[MemeMemPlus] Gemini API 异常: {traceback.format_exc()}")
            return None

    def _parse_gemini_response(self, response_data: dict) -> bytes | None:
        """从 Gemini 响应中提取生成的图片。"""
        candidates = response_data.get("candidates", [])
        if not candidates:
            feedback = response_data.get("promptFeedback")
            if feedback:
                logger.warning(f"[MemeMemPlus] 请求被阻止: {feedback}")
            else:
                logger.warning(f"[MemeMemPlus] Gemini 响应无 candidates, 完整响应: {str(response_data)[:500]}")
            return None

        for candidate in candidates:
            finish_reason = candidate.get("finishReason")
            if finish_reason in ("SAFETY", "RECITATION"):
                logger.warning(f"[MemeMemPlus] 生成被安全策略阻止: {finish_reason}")
                continue

            content = candidate.get("content", {})
            parts = content.get("parts", [])

            # 记录所有 parts 的 key，方便排查
            part_keys = [list(p.keys()) for p in parts]
            logger.debug(f"[MemeMemPlus] Gemini parts 结构: {part_keys}, finishReason={finish_reason}")

            for part in parts:
                # 如果返回了文本而非图片，记录下来
                if "text" in part and not part.get("inlineData") and not part.get("inline_data"):
                    logger.debug(f"[MemeMemPlus] Gemini 返回了文本: {str(part['text'])[:200]}")

                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data and not part.get("thought", False):
                    b64_data = inline_data.get("data", "")
                    if b64_data:
                        try:
                            image_bytes = base64.b64decode(b64_data)
                            logger.debug(f"[MemeMemPlus] Gemini 图片生成成功 ({len(image_bytes) // 1024}KB)")
                            return image_bytes
                        except Exception:
                            logger.error("[MemeMemPlus] base64 解码失败")
                            continue

        logger.warning("[MemeMemPlus] Gemini 响应中未找到图片数据")
        return None

    # ── Grok 生图 ────────────────────────────────────────────────

    @staticmethod
    def _compress_image(image_data: bytes, max_size: int = 800, quality: int = 80) -> tuple[bytes, str]:
        """压缩图片到指定最大边长和质量，返回 (bytes, mime_type)。"""
        img = Image.open(io.BytesIO(image_data))
        # 缩放
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        # 转 RGB（去掉 alpha）
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"

    async def _generate_grok(self, mood: str, reference_paths: list[Path] | None) -> bytes | None:
        has_refs = bool(reference_paths)

        prompt = self.settings.image_prompt_template.replace("{mood}", mood)
        if has_refs:
            addon = self.settings.reference_prompt_addon.replace("{mood}", mood)
            prompt += "\n" + addon

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Grok 分辨率映射
        res_map = {"1K": "1k", "2K": "2k", "4K": "2k"}
        resolution = res_map.get(self.settings.resolution, "1k")

        if has_refs:
            # 图编辑模式: POST /images/edits（Grok 仅支持 1 张输入图）
            url = f"{self._api_base}/images/edits"
            # 从存在的参考图中随机选一张
            valid_refs = [p for p in reference_paths if p.exists()]
            if not valid_refs:
                return await self._grok_text2img(prompt, headers, resolution)
            ref_path = random.choice(valid_refs)
            try:
                raw_data = ref_path.read_bytes()
                compressed, mime_type = await asyncio.to_thread(self._compress_image, raw_data)
                b64_data = base64.b64encode(compressed).decode("utf-8")
                data_uri = f"data:{mime_type};base64,{b64_data}"
            except Exception:
                logger.warning(f"[MemeMemPlus] 读取/压缩参考图失败: {ref_path.name}")
                return await self._grok_text2img(prompt, headers, resolution)

            logger.debug(f"[MemeMemPlus] Grok 图编辑模式: {ref_path.name}")
            payload = {
                "model": self._model,
                "prompt": prompt,
                "image": {"url": data_uri, "type": "image_url"},
                "response_format": "b64_json",
                "aspect_ratio": self.settings.aspect_ratio,
                "resolution": resolution,
            }
        else:
            logger.debug(f"[MemeMemPlus] Grok 文生图: mood={mood}")
            return await self._grok_text2img(prompt, headers, resolution)

        try:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout)
            session = await LLMClient.get_session()
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[MemeMemPlus] Grok API 错误 {resp.status}: {error_text[:200]}")
                    return None
                data = await resp.json()
                return self._parse_grok_response(data)
        except aiohttp.ClientError as e:
            logger.error(f"[MemeMemPlus] Grok API 网络错误: {e}")
            return None
        except Exception:
            logger.error(f"[MemeMemPlus] Grok API 异常: {traceback.format_exc()}")
            return None

    async def _grok_text2img(self, prompt: str, headers: dict, resolution: str) -> bytes | None:
        """Grok 文生图: POST /images/generations"""
        url = f"{self._api_base}/images/generations"
        payload = {
            "model": self._model,
            "prompt": prompt,
            "response_format": "b64_json",
            "aspect_ratio": self.settings.aspect_ratio,
            "resolution": resolution,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout)
            session = await LLMClient.get_session()
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[MemeMemPlus] Grok API 错误 {resp.status}: {error_text[:200]}")
                    return None
                data = await resp.json()
                return self._parse_grok_response(data)
        except aiohttp.ClientError as e:
            logger.error(f"[MemeMemPlus] Grok API 网络错误: {e}")
            return None
        except Exception:
            logger.error(f"[MemeMemPlus] Grok API 异常: {traceback.format_exc()}")
            return None

    def _parse_grok_response(self, response_data: dict) -> bytes | None:
        """从 Grok 响应中提取图片（OpenAI images 格式）。仅处理 b64_json。"""
        data_list = response_data.get("data", [])
        if not data_list:
            logger.warning(f"[MemeMemPlus] Grok 响应无 data: {str(response_data)[:200]}")
            return None

        item = data_list[0]
        b64 = item.get("b64_json", "")
        if b64:
            try:
                image_bytes = base64.b64decode(b64)
                logger.debug(f"[MemeMemPlus] Grok 图片生成成功 ({len(image_bytes) // 1024}KB)")
                return image_bytes
            except Exception:
                logger.error("[MemeMemPlus] Grok base64 解码失败")
                return None

        # URL 模式不应出现（我们请求了 b64_json），但记录日志
        img_url = item.get("url", "")
        if img_url:
            logger.warning("[MemeMemPlus] Grok 返回了 URL 而非 b64_json，请检查 response_format 配置")

        logger.warning("[MemeMemPlus] Grok 响应中未找到图片数据")
        return None
