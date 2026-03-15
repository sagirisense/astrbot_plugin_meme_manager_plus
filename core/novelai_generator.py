"""NovelAI 角色扮演生图模块。

独立于心情表情流程，根据 Bot 对话内容 + 用户预设角色标签，
调用 LLM 补全场景/表情标签，然后调用 NovelAI API 生成图片。
支持 Vibe Transfer：在配置面板上传参考图提取角色特征作为语义引导。
"""

import base64
import hashlib
import random
import zipfile
import io
import traceback
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ..utils.provider_helper import LLMApiConfig, load_mood_provider
from ..utils.llm_client import LLMClient


NAI_API_URL = "https://image.novelai.net/ai/generate-image"

DEFAULT_TAG_PROMPT = (
    "You are a NovelAI tag generator for anime character illustrations.\n"
    "Given the character's base tags and the current dialogue context, "
    "generate supplementary tags to complete the image description.\n\n"
    "Character base tags (DO NOT repeat these): {base_tags}\n"
    "Current dialogue content: {bot_reply}\n\n"
    "Generate tags for:\n"
    "- Facial expression (e.g. smile, blush, angry, crying, pout)\n"
    "- Pose/action (e.g. sitting, standing, looking_at_viewer, hand_on_hip)\n"
    "- Scene/background (e.g. outdoors, bedroom, classroom, night sky)\n"
    "- Other modifiers (e.g. wind, light_particles, from_above)\n\n"
    "Output ONLY comma-separated tags, no explanation, no base tags.\n"
    "Example: smile, looking_at_viewer, sitting, classroom, hand_on_chin"
)


class NovelAIGenerator:
    """NovelAI 生图器：LLM 补全标签 → NAI API 生图 → 保存。"""

    def __init__(self, settings, context, plugin_dir: Path):
        self.settings = settings
        self.context = context
        self.output_dir = plugin_dir / "novelai"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 参考图从配置面板上传，存放在 files/novelai_reference_image/ 目录
        self._ref_dir = plugin_dir / "files" / "novelai_reference_image"

    @property
    def has_reference(self) -> bool:
        return self._find_reference() is not None

    def _find_reference(self) -> Path | None:
        """从配置上传目录中找到第一张参考图。"""
        if not self._ref_dir.exists():
            return None
        exts = (".jpg", ".jpeg", ".png", ".webp")
        for f in self._ref_dir.iterdir():
            if f.is_file() and f.suffix.lower() in exts:
                return f
        return None

    def _load_reference_b64(self) -> str | None:
        """读取参考图并返回 base64 字符串。"""
        ref = self._find_reference()
        if not ref:
            return None
        try:
            return base64.b64encode(ref.read_bytes()).decode()
        except Exception:
            logger.warning(f"[MemeMemPlus-NAI] 读取参考图失败: {traceback.format_exc()}")
            return None

    async def run(self, bot_reply: str) -> tuple[bytes | None, str | None]:
        """完整流程：补全标签 → 生图 → 保存。

        Returns:
            (image_bytes, saved_path) 或 (None, None) 失败时。
        """
        cfg = load_mood_provider(self.context, self.settings)
        if not cfg.valid:
            logger.warning("[MemeMemPlus-NAI] 未找到 LLM 提供商配置，无法补全标签")
            return None, None

        base_tags = self.settings.novelai_base_tags.strip()
        if not base_tags:
            logger.warning("[MemeMemPlus-NAI] 未配置角色基础标签")
            return None, None

        # 1. LLM 补全标签
        extra_tags = await self._generate_tags(cfg, bot_reply, base_tags)
        if not extra_tags:
            logger.warning("[MemeMemPlus-NAI] LLM 标签补全失败，使用基础标签生图")
            full_tags = base_tags
        else:
            full_tags = f"{base_tags}, {extra_tags}"

        logger.info(f"[MemeMemPlus-NAI] 最终标签: {full_tags[:200]}")

        # 2. 调用 NovelAI API
        image_bytes = await self._call_nai_api(full_tags)
        if not image_bytes:
            return None, None

        # 3. 保存到 novelai/ 目录
        name_hash = hashlib.md5(image_bytes).hexdigest()[:12]
        save_path = self.output_dir / f"nai_{name_hash}.png"
        try:
            save_path.write_bytes(image_bytes)
            logger.info(f"[MemeMemPlus-NAI] 图片已保存: {save_path.name}")
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 保存失败: {traceback.format_exc()}")

        return image_bytes, str(save_path)

    async def _generate_tags(
        self, cfg: LLMApiConfig, bot_reply: str, base_tags: str
    ) -> str | None:
        """调用 LLM 根据对话内容补全角色标签。"""
        template = self.settings.novelai_tag_prompt or DEFAULT_TAG_PROMPT
        prompt = template.replace("{base_tags}", base_tags).replace(
            "{bot_reply}", bot_reply[:500]
        )
        system_msg = (
            "You are a tag generator. Output ONLY comma-separated English tags. "
            "No explanation, no numbering, no markdown."
        )

        try:
            result = await LLMClient.call(
                cfg,
                prompt,
                system_msg=system_msg,
                max_tokens=150,
                timeout=self.settings.llm_timeout,
            )
            if result:
                # 清理：去掉可能的引号、换行、编号
                cleaned = result.strip().strip('"\'').replace("\n", ", ")
                logger.info(f"[MemeMemPlus-NAI] LLM 补全标签: {cleaned[:150]}")
                return cleaned
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 标签补全异常: {traceback.format_exc()}")
        return None

    async def _call_nai_api(self, tags: str) -> bytes | None:
        """调用 NovelAI Image Generation API。"""
        api_key = self.settings.novelai_api_key
        if not api_key:
            logger.error("[MemeMemPlus-NAI] 未配置 NovelAI API Key")
            return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        model = self.settings.novelai_model
        negative = self.settings.novelai_negative_prompt
        is_v4 = "nai-diffusion-4" in model  # 匹配 V4 和 V4.5

        s = self.settings
        seed = s.novelai_seed if s.novelai_seed >= 0 else random.randint(0, 2**32 - 1)

        # 通用参数（所有模型都支持）
        params: dict = {
            "width": s.novelai_width,
            "height": s.novelai_height,
            "scale": s.novelai_scale,
            "sampler": s.novelai_sampler,
            "steps": s.novelai_steps,
            "n_samples": 1,
            "seed": seed,
            "negative_prompt": negative,
            "qualityToggle": s.novelai_quality_toggle,
            "ucPreset": s.novelai_uc_preset,
        }

        if is_v4:
            # V4/V4.5 专用参数
            params["params_version"] = 3
            params["noise_schedule"] = s.novelai_noise_schedule
            params["cfg_rescale"] = s.novelai_cfg_rescale
            params["dynamic_thresholding"] = s.novelai_dynamic_thresholding
            params["sm"] = False  # V4 不支持 SMEA
            params["sm_dyn"] = False
            params["legacy"] = False
            params["use_coords"] = False
            params["characterPrompts"] = []
            params["v4_prompt"] = {
                "caption": {
                    "base_caption": tags,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order": True,
            }
            params["v4_negative_prompt"] = {
                "caption": {
                    "base_caption": negative,
                    "char_captions": [],
                },
            }
            # Variety Boost (V4+ 专用)
            if s.novelai_variety_boost > 0:
                params["skip_cfg_above_sigma"] = s.novelai_variety_boost
        else:
            # V3 专用参数
            params["sm"] = s.novelai_smea
            params["sm_dyn"] = s.novelai_smea_dyn

        # 参考图：需开启 use_reference 开关
        action = "generate"
        ref_b64 = None

        if s.novelai_use_reference:
            ref_b64 = self._load_reference_b64()
            if ref_b64:
                ref_mode = s.novelai_reference_mode
                is_v45 = "nai-diffusion-4-5" in model

                # director 模式需要 V4.5，不支持时自动降级为 vibe_transfer
                if ref_mode == "director" and not is_v45:
                    logger.warning(
                        f"[MemeMemPlus-NAI] Precise Reference 仅支持 V4.5 模型，"
                        f"当前模型 {model}，自动降级为 Vibe Transfer"
                    )
                    ref_mode = "vibe_transfer"

                if ref_mode == "img2img":
                    # img2img 模式：以参考图为底图直接变换
                    action = "img2img"
                    params["strength"] = s.novelai_img2img_strength
                    params["noise"] = s.novelai_img2img_noise
                    params["extra_noise_seed"] = random.randint(0, 2**32 - 1)
                    logger.info(
                        f"[MemeMemPlus-NAI] 使用参考图 img2img "
                        f"(strength={s.novelai_img2img_strength}, noise={s.novelai_img2img_noise})"
                    )
                elif ref_mode == "director":
                    # Director / Precise Reference 模式（仅 V4.5）
                    params["director_reference_images"] = [ref_b64]
                    params["director_reference_strength_values"] = [s.novelai_director_strength]
                    params["director_reference_secondary_strength_values"] = [s.novelai_director_fidelity]
                    params["director_reference_information_extracted"] = [s.novelai_director_info_extracted]
                    params["director_reference_descriptions"] = [{
                        "base_caption": tags,
                        "char_captions": [],
                    }]
                    logger.info(
                        f"[MemeMemPlus-NAI] 使用参考图 Precise Reference "
                        f"(strength={s.novelai_director_strength}, "
                        f"fidelity={s.novelai_director_fidelity})"
                    )
                else:
                    # Vibe Transfer 模式：提取特征引导生图
                    params["reference_image_multiple"] = [ref_b64]
                    params["reference_information_extracted_multiple"] = [s.novelai_reference_info_extracted]
                    params["reference_strength_multiple"] = [s.novelai_reference_strength]
                    logger.info(
                        f"[MemeMemPlus-NAI] 使用参考图 Vibe Transfer "
                        f"(strength={s.novelai_reference_strength}, "
                        f"info={s.novelai_reference_info_extracted})"
                    )
            else:
                logger.info("[MemeMemPlus-NAI] 参考图已开启但未找到图片，纯文生图")

        payload: dict = {
            "input": tags,
            "model": model,
            "action": action,
            "parameters": params,
        }
        # img2img 需要在 payload 顶层放 image
        if action == "img2img":
            payload["image"] = ref_b64

        try:
            tm = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    NAI_API_URL, headers=headers, json=payload, timeout=tm
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            f"[MemeMemPlus-NAI] API 错误 {resp.status}: {error_text[:300]}"
                        )
                        return None

                    # NAI 返回的是 zip 文件，里面包含生成的图片
                    data = await resp.read()
                    return self._extract_image_from_zip(data)

        except aiohttp.ClientError as e:
            logger.error(f"[MemeMemPlus-NAI] 网络错误: {type(e).__name__}: {e}")
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] API 调用异常: {traceback.format_exc()}")
        return None

    @staticmethod
    def _extract_image_from_zip(data: bytes) -> bytes | None:
        """从 NAI 返回的 zip 数据中提取图片。"""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        return zf.read(name)
            logger.warning("[MemeMemPlus-NAI] zip 中未找到图片文件")
        except zipfile.BadZipFile:
            logger.error("[MemeMemPlus-NAI] 返回数据不是有效的 zip 文件")
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 解析响应失败: {traceback.format_exc()}")
        return None
