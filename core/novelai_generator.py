"""NovelAI 角色扮演生图模块。

独立于心情表情流程，根据 Bot 对话内容 + 用户预设角色标签，
调用 LLM 补全场景/表情标签，然后调用 NovelAI API 生成图片。
支持 Vibe Transfer：在配置面板上传参考图提取角色特征作为语义引导。
"""

import base64
import collections
import hashlib
import json
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

SFW_TAG_ADDON = (
    "\n\n**ECCHI LIMIT RULES (HIGHEST PRIORITY, MUST OBEY):**\n"
    "- Mild skin exposure is ALLOWED: bare_shoulders, off_shoulder, midriff, "
    "bikini, swimsuit, shorts, miniskirt, cleavage, sleeveless, tank_top, "
    "sundress, crop_top, thighhighs, etc. These are all fine.\n"
    "- ABSOLUTELY FORBIDDEN (hard ban): "
    "nude, naked, topless, bottomless, no_bra, no_panties, nipples, pussy, penis, "
    "sex, cum, ahegao, spread_legs, bondage, tentacles, see-through_nipples, "
    "any genital exposure, any sexual action tags, any body fluid tags.\n"
    "- Rule of thumb: underwear/bra must always be worn. "
    "Showing skin is OK, showing what's under the underwear is NOT.\n"
    "- Even if the dialogue is suggestive, do NOT cross into explicit territory.\n"
)

R18_TAG_ADDON = (
    "\n\n**CRITICAL RULES（最高优先级，必须严格遵守）：**\n"
    "- 如果用户对话中出现「全裸」「completely nude」「naked」「stripped」「无衣」「裸体」「裸身」或任何类似词汇，**强制**使用 completely_nude, naked, no_clothes, no_panties, no_bra, stripped, nude, bottomless, topless，并且**绝对禁止**生成任何衣服相关 tag（包括 torn_clothes、lifted_skirt、open_shirt、bikini、panties 等全部排除）。\n"
    "- 输出必须**极度黄暴**（very explicit & hardcore），优先使用最直接、最淫乱、最暴力的 tag，绝不使用温和描述。\n"
    "- 如果对话中出现「丈夫」「老公」「husband」「 hubby」「夫君」或任何夫妻做爱相关描述，必须**自动提取并加入丈夫专属做爱模式 tag**（例如 sex_with_husband, husband_penetration, married_sex, creampie_from_husband, passionate_rough_sex 等），并结合具体体位和插入细节。\n\n"

    "Tag categories（根据对话上下文尽可能多地使用，优先 hardcore）：\n"
    "- Nudity（强制全裸时必须放最前面）：completely_nude, naked, no_clothes, stripped, bottomless, topless, pussy_exposed, asshole_visible, nipples_exposed\n"
    "- Body features（加强）：huge_breasts, large_breasts, erect_nipples, puffy_nipples, thick_thighs, wide_hips, slim_waist, plump_ass, detailed_pussy, spread_pussy, clitoris_visible, dripping_pussy\n"
    "- Sexual actions & Husband Sex Modes（重点新增！）：sex_with_husband, husband_penetration, married_sex, creampie_from_husband, vaginal_sex_with_husband, anal_sex_with_husband, oral_sex_with_husband, deep_penetration, rough_sex, passionate_sex, missionary_position, doggystyle, cowgirl_position, reverse_cowgirl, mating_press, full_nelson, legs_up, spread_legs, double_penetration, triple_penetration, breeding, impregnation_risk, breast_sucking, licking_nipple, nipple_stimulation\n"
    "- Fantasy/fetish（如果对话暗示则加入）：tentacles, tentacle_sex, bondage, restrained, collar, leash, blindfold, aphrodisiac, oviposition, cum_inflation, belly_bulge\n"
    "- Body states（必须加）：ahegao, rolling_eyes, tongue_out, drooling, tears, crying_with_eyes_open, heavy_breathing, trembling, sweat, wet_body, cum_on_body, cum_in_pussy, cum_in_ass, cum_in_mouth, pussy_juice, tentacle_cum, overflowing_cum\n"
    "- Clothing state（仅当用户明确要求穿衣时使用，否则彻底跳过）：绝对不加任何衣服 tag\n\n"

    "生成逻辑：\n"
    "1. 先提取用户对话中的关键元素（全裸/丈夫/体位/暴力程度）。\n"
    "2. 强制把全裸 tag 放在最前面。\n"
    "3. 如果是丈夫做爱场景，必须加入 sex_with_husband + 具体体位 + 射精/中出细节。\n"
    "4. 整体输出要**很黄很暴力**，直接、淫乱、无底线。\n"
    "5. 最终 tags 直接可用于 NovelAI img2img 生成图（已自动兼容你之前用的触手/森林风格）。\n\n"

    "Mix these NSFW tags naturally with expression/pose/scene tags.\n\n"

    "**OUTPUT FORMAT (you MUST follow this exactly):**\n"
    "First line: comma-separated POSITIVE tags (what to draw)\n"
    "Second line: NEGATIVE: comma-separated NEGATIVE tags (what to avoid)\n"
    "The NEGATIVE line lists elements that would HURT the image quality or contradict the scene.\n"
    "Examples of negative tags: clothed, dress, skirt, pants (when nude scene), "
    "ugly, deformed, bad_anatomy, extra_limbs, missing_fingers, blurry, "
    "censored, mosaic_censoring, bar_censor, text, watermark, "
    "multiple_boys (when solo scene), flat_chest (when large_breasts intended)\n\n"
    "Example output:\n"
    "completely_nude, huge_breasts, spread_legs, ahegao, sweat\n"
    "NEGATIVE: clothed, dress, censored, flat_chest, bad_anatomy, blurry"
)


class NovelAIGenerator:
    """NovelAI 生图器：LLM 补全标签 → NAI API 生图 → 保存。"""

    def __init__(self, settings, context, plugin_dir: Path):
        self.settings = settings
        self.context = context
        self.output_dir = plugin_dir / "novelai"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir = self.output_dir / "generated"
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        # 参考图从配置面板上传，存放在 files/novelai_reference_image/ 目录
        self._ref_dir = plugin_dir / "files" / "novelai_reference_image"
        # 标签历史缓存：按会话隔离，每个 session_id 独立的 deque
        self._tag_history: dict[str, collections.deque[str]] = {}
        self._tag_cache_dir = plugin_dir / "tag_cache"
        self._tag_cache_dir.mkdir(parents=True, exist_ok=True)
        self._load_all_tag_caches()
        # life_scheduler 插件实例缓存
        self._life_plugin = None
        # 穿搭 tag 缓存：仅在穿搭文本变化时重新生成
        self._cached_outfit_text: str = ""
        self._cached_outfit_tags: str = ""

    def _get_session_history(self, session_id: str) -> collections.deque:
        """获取指定会话的标签历史 deque，不存在则创建。"""
        if session_id not in self._tag_history:
            self._tag_history[session_id] = collections.deque()
        return self._tag_history[session_id]

    def _session_cache_path(self, session_id: str) -> Path:
        """会话缓存文件路径：tag_cache/<safe_name>.json"""
        safe = hashlib.md5(session_id.encode()).hexdigest()[:12]
        return self._tag_cache_dir / f"{safe}.json"

    def _save_session_cache(self, session_id: str) -> None:
        """将指定会话的标签历史持久化到文件。最多保存最近 500 条防止文件过大。"""
        if not session_id:
            return
        q = self._tag_history.get(session_id)
        if not q:
            return
        try:
            tags = list(q)[-500:]  # 磁盘保留上限，内存保持完整
            data = {"session_id": session_id, "tags": tags}
            self._session_cache_path(session_id).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_all_tag_caches(self) -> None:
        """启动时从 tag_cache/ 恢复所有会话历史。"""
        if not self._tag_cache_dir.exists():
            return
        for f in self._tag_cache_dir.iterdir():
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sid = data.get("session_id", "")
                tags = data.get("tags", [])
                if sid and tags:
                    q = collections.deque(tags)
                    self._tag_history[sid] = q
                    logger.debug(f"[MemeMemPlus-NAI] 恢复会话标签历史: {sid[:20]}... ({len(q)} 条)")
            except Exception:
                pass

    def _get_raw_outfit(self) -> str | None:
        """从 life_scheduler 插件获取今日穿搭原始文本。"""
        if not self._life_plugin:
            try:
                for p in self.context.get_all_stars():
                    p_id = getattr(p, "id", "") or ""
                    p_name = getattr(p, "name", "") or ""
                    if "life_scheduler" in p_id or "life_scheduler" in p_name:
                        self._life_plugin = (
                            getattr(p, "star_instance", None)
                            or getattr(p, "instance", None)
                            or getattr(p, "star_cls", None)
                        )
                        break
            except Exception:
                return None
        if not self._life_plugin:
            return None
        try:
            import datetime as _dt
            data_mgr = getattr(self._life_plugin, "data_mgr", None)
            if not data_mgr:
                return None
            schedule = data_mgr.get(_dt.datetime.now())
            if not schedule or getattr(schedule, "status", "") == "failed":
                return None
            outfit = getattr(schedule, "outfit", "") or ""
            outfit_style = getattr(schedule, "outfit_style", "") or ""
            parts = []
            if outfit:
                parts.append(outfit)
            if outfit_style:
                parts.append(outfit_style)
            return ", ".join(parts) if parts else None
        except Exception as e:
            logger.debug(f"[MemeMemPlus-NAI] 获取穿搭失败: {e}")
            return None

    async def _refresh_outfit_tags(self, cfg) -> str:
        """检查穿搭是否变化，变化时用 LLM 转换为 NovelAI tags 并缓存。返回缓存的 tags。"""
        raw = self._get_raw_outfit() or ""
        if raw == self._cached_outfit_text:
            return self._cached_outfit_tags
        # 穿搭变化
        logger.debug(f"[MemeMemPlus-NAI] 穿搭变化检测: 旧='{self._cached_outfit_text[:30]}' 新='{raw[:30]}'")
        self._cached_outfit_text = raw
        if not raw:
            self._cached_outfit_tags = ""
            logger.info("[MemeMemPlus-NAI] 穿搭已清空")
            return ""
        # LLM 转换穿搭描述为 NovelAI tags
        try:
            prompt = (
                f"Convert this outfit description to NovelAI image tags (comma-separated English tags only):\n"
                f"{raw}\n\n"
                f"Output ONLY comma-separated clothing/accessory tags. No explanation."
            )
            result = await LLMClient.call(
                cfg, prompt,
                system_msg="You are a tag converter. Output ONLY comma-separated English tags.",
                max_tokens=100,
                timeout=self.settings.llm_timeout,
            )
            # 多行输出合并为逗号分隔的单行
            if result:
                lines = [l.strip().rstrip(",").strip() for l in result.splitlines() if l.strip()]
                tags = ", ".join(lines).rstrip(",").strip()
            else:
                tags = ""
            self._cached_outfit_tags = tags
            logger.info(f"[MemeMemPlus-NAI] 穿搭 tag 已更新: '{raw[:30]}...' → '{tags}'")
        except Exception as e:
            logger.warning(f"[MemeMemPlus-NAI] 穿搭 tag 转换失败: {e}")
            self._cached_outfit_tags = ""
        return self._cached_outfit_tags

    def clear_tag_caches(self) -> None:
        """清空标签历史和穿搭缓存。外部可在 /reset 等场景调用。"""
        self._tag_history.clear()
        self._cached_outfit_text = ""
        self._cached_outfit_tags = ""
        # 清除持久化文件
        if self._tag_cache_dir.exists():
            for f in self._tag_cache_dir.iterdir():
                if f.suffix == ".json":
                    try:
                        f.unlink()
                    except Exception:
                        pass
        logger.info("[MemeMemPlus-NAI] 标签历史和穿搭缓存已清空")

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

    def _enforce_cache_limit(self) -> None:
        """如果 novelai/ 及 generated/ 目录图片数超过上限，删除最旧的图片腾出空间。"""
        max_size = getattr(self.settings, "novelai_max_cache", 0)
        if max_size <= 0:
            return
        exts = (".jpg", ".jpeg", ".png", ".webp")
        # 扫描 output_dir 和 generated_dir 两个目录
        all_files = []
        for d in (self.output_dir, self.generated_dir):
            if d.exists():
                all_files.extend(
                    f for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in exts
                )
        files = sorted(all_files, key=lambda f: f.stat().st_mtime if f.exists() else 0)
        # 需要删除的数量（为新图腾出 1 个位置）
        to_remove = len(files) - max_size + 1
        if to_remove <= 0:
            return
        for f in files[:to_remove]:
            try:
                f.unlink()
                logger.info(f"[MemeMemPlus-NAI] 缓存超限，已删除最旧图片: {f.name}")
            except Exception:
                logger.warning(f"[MemeMemPlus-NAI] 删除失败: {f.name}")

    def _load_reference_b64(self) -> str | None:
        """读取参考图并返回 base64 字符串。"""
        ref = self._find_reference()
        if not ref:
            return None
        try:
            return base64.b64encode(ref.read_bytes()).decode()
        except Exception:
            logger.warning(
                f"[MemeMemPlus-NAI] 读取参考图失败: {traceback.format_exc()}")
            return None

    async def run(self, bot_reply: str, session_id: str = "") -> tuple[bytes | None, str | None]:
        """完整流程：补全标签 → 生图 → 保存。

        Returns:
            (image_bytes, saved_path) 或 (None, None) 失败时。
        """
        base_tags = self.settings.novelai_base_tags.strip()
        if not base_tags:
            logger.warning("[MemeMemPlus-NAI] 未配置角色基础标签")
            return None, None

        extra_negative = None
        llm_enabled = getattr(self.settings, "novelai_llm_enabled", True)

        if llm_enabled:
            # LLM 模式：用 LLM 根据对话内容补全标签
            cfg = load_mood_provider(self.context, self.settings)
            if not cfg.valid:
                logger.warning("[MemeMemPlus-NAI] 未找到 LLM 提供商配置，无法补全标签")
                return None, None
            extra_tags, extra_negative = await self._generate_tags(cfg, bot_reply, base_tags, session_id)
            if not extra_tags:
                logger.warning("[MemeMemPlus-NAI] LLM 标签补全失败，使用基础标签生图")
                full_tags = base_tags
            else:
                full_tags = f"{base_tags}, {extra_tags}"
        else:
            # 纯标签模式：不调用 LLM，直接用 base_tags
            logger.info("[MemeMemPlus-NAI] LLM 已关闭，使用基础标签直接生图")
            full_tags = base_tags

        # 追加用户自定义标签
        custom_tags = self.settings.novelai_custom_tags.strip()
        if custom_tags:
            full_tags = f"{full_tags}, {custom_tags}"
        # R18 模式追加专用自定义标签
        if self.settings.novelai_r18:
            r18_custom = self.settings.novelai_r18_custom_tags.strip()
            if r18_custom:
                full_tags = f"{full_tags}, {r18_custom}"

        # 穿搭 tags：直接拼接到末尾，用 (tag:weight) 控制权重
        if getattr(self.settings, "novelai_use_outfit", False):
            if not llm_enabled:
                # 穿搭需要 LLM 转换，非 LLM 模式时跳过
                logger.debug("[MemeMemPlus-NAI] LLM 已关闭，跳过穿搭 tag 注入")
                outfit_tags = ""
            else:
                outfit_tags = await self._refresh_outfit_tags(cfg)
            if outfit_tags:
                ow = getattr(self.settings, "novelai_outfit_weight", 0.85)
                if abs(ow - 1.0) < 0.01:
                    # 权重为 1.0 时不加权重标记
                    full_tags = f"{full_tags}, {outfit_tags}"
                else:
                    weighted = ", ".join(f"({t.strip()}:{ow})" for t in outfit_tags.split(",") if t.strip())
                    full_tags = f"{full_tags}, {weighted}"

        logger.info(f"[MemeMemPlus-NAI] 最终正向标签: {full_tags}")
        if extra_negative:
            logger.info(
                f"[MemeMemPlus-NAI] 最终负向标签: {self.settings.novelai_negative_prompt}, {extra_negative}")

        # 2. 调用 NovelAI API（extra_negative 会追加到配置的负向标签后面）
        image_bytes = await self._call_nai_api(full_tags, extra_negative=extra_negative)
        if not image_bytes:
            return None, None

        save_path = self._save_image(image_bytes)
        return image_bytes, str(save_path) if save_path else None

    async def run_direct(
        self, positive_tags: str, model_override: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """直接用用户提供的正向标签生图，负向标签用配置值。跳过 LLM。

        Args:
            positive_tags: 用户提供的正向标签。
            model_override: 覆盖模型名称，None 使用配置默认值。

        Returns:
            (image_bytes, saved_path) 或 (None, None) 失败时。
        """
        if not positive_tags.strip():
            logger.warning("[MemeMemPlus-NAI] /ni 命令未提供标签")
            return None, None

        logger.info(f"[MemeMemPlus-NAI] /ni 直接生图, 正向标签: {positive_tags}, 模型: {model_override or '默认'}")
        image_bytes = await self._call_nai_api(positive_tags.strip(), model_override=model_override)
        if not image_bytes:
            return None, None

        save_path = self._save_image(image_bytes, subdir=self.generated_dir)
        return image_bytes, str(save_path) if save_path else None

    def _save_image(self, image_bytes: bytes, subdir: Path | None = None) -> Path | None:
        """保存生成的图片，超过上限时删除最旧的。

        Args:
            subdir: 保存目录，默认为 novelai/。/ni 命令使用 novelai/generated/。
        """
        self._enforce_cache_limit()
        target_dir = subdir or self.output_dir
        name_hash = hashlib.md5(image_bytes).hexdigest()[:12]
        save_path = target_dir / f"nai_{name_hash}.png"
        try:
            save_path.write_bytes(image_bytes)
            logger.info(f"[MemeMemPlus-NAI] 图片已保存: {save_path.name}")
            return save_path
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 保存失败: {traceback.format_exc()}")
            return None

    async def _generate_tags(
        self, cfg: LLMApiConfig, bot_reply: str, base_tags: str, session_id: str = ""
    ) -> tuple[str | None, str | None]:
        """调用 LLM 根据对话内容补全角色标签。

        Returns:
            (positive_tags, negative_tags) — R18 模式下会解析 NEGATIVE: 行，
            非 R18 模式 negative_tags 始终为 None。
        """
        template = self.settings.novelai_tag_prompt or DEFAULT_TAG_PROMPT
        prompt = template.replace("{base_tags}", base_tags).replace(
            "{bot_reply}", bot_reply[:500]
        )
        # 注入标签历史上下文，使画面风格连贯（越旧权重越低，按会话隔离）
        history_size = getattr(self.settings, "novelai_tag_history_size", 5)
        max_weight = getattr(self.settings, "novelai_history_weight", 0.8)
        session_history = self._get_session_history(session_id) if session_id else None
        if history_size > 0 and session_history:
            recent = list(session_history)[-history_size:]
            n = len(recent)
            min_weight = 0.3
            history_lines = []
            for i, t in enumerate(recent):
                weight = round(min_weight + (max_weight - min_weight) * (i / max(n - 1, 1)), 2) if n > 1 else round(max_weight * 0.6, 2)
                history_lines.append(f"  [{i+1}] (weight {weight}): {t}")
            prompt += (
                f"\n\nRecent generated tags (for gradual visual continuity, NOT for copying). "
                f"Weights indicate how much to reference — {max_weight} = light hint, {min_weight} = barely consider:\n"
                + "\n".join(history_lines) + "\n"
            )
        is_r18 = self.settings.novelai_r18
        # R18 模式：追加 NSFW 标签生成指令（含 NEGATIVE 行输出格式）
        if is_r18:
            prompt += R18_TAG_ADDON
        elif getattr(self.settings, "novelai_safe_mode", True):
            # 安全模式：强制 SFW 约束，防止 LLM 生成擦边标签
            prompt += SFW_TAG_ADDON
        # 安全模式关闭且非 R18：LLM 自由生成，不追加任何约束
        system_msg = (
            "You are a tag generator. Output ONLY comma-separated English tags. "
            "No explanation, no numbering, no markdown."
        )

        logger.debug(f"[MemeMemPlus-NAI] 标签生成提示词:\n{prompt}")

        try:
            result = await LLMClient.call(
                cfg,
                prompt,
                system_msg=system_msg,
                max_tokens=200,
                timeout=self.settings.llm_timeout,
            )
            if result:
                logger.debug(f"[MemeMemPlus-NAI] LLM 原始输出:\n{result}")
                positive, negative = self._parse_tag_result(result, is_r18)
                # 缓存正向标签供后续生图参考（仅在标签历史功能开启时，按会话隔离）
                sh = self._get_session_history(session_id) if session_id else None
                logger.debug(f"[MemeMemPlus-NAI] 标签解析结果: positive={bool(positive)}, history_size={history_size}, 当前历史={len(sh) if sh else 0}")
                if positive and history_size > 0 and sh is not None:
                    sh.append(positive)
                    self._save_session_cache(session_id)
                    logger.debug(f"[MemeMemPlus-NAI] 标签已入栈, 历史数={len(sh)}")
                return positive, negative
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 标签补全异常: {traceback.format_exc()}")
        return None, None

    @staticmethod
    def _parse_tag_result(result: str, is_r18: bool) -> tuple[str | None, str | None]:
        """解析 LLM 标签输出，分离正向和负向标签。"""
        positive = None
        negative = None

        lines = result.strip().strip('"\'').splitlines()
        pos_parts = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 检测 NEGATIVE: 前缀（不区分大小写）
            lower = stripped.lower()
            if lower.startswith("negative:"):
                neg_text = stripped[len("negative:"):].strip().strip(',')
                if neg_text:
                    negative = neg_text
            else:
                pos_parts.append(stripped.rstrip(',').strip())

        if pos_parts:
            positive = ", ".join(pos_parts)

        if not negative and is_r18:
            logger.debug("[MemeMemPlus-NAI] LLM 未输出 NEGATIVE 行，仅使用配置负向标签")

        return positive, negative

    async def _call_nai_api(
        self, tags: str, extra_negative: str | None = None,
        model_override: str | None = None,
    ) -> bytes | None:
        """调用 NovelAI Image Generation API。"""
        api_key = self.settings.novelai_api_key
        if not api_key:
            logger.error("[MemeMemPlus-NAI] 未配置 NovelAI API Key")
            return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        model = model_override or self.settings.novelai_model
        negative = self.settings.novelai_negative_prompt
        # 追加 LLM 动态生成的负向标签
        if extra_negative:
            negative = f"{negative}, {extra_negative}" if negative else extra_negative
        is_v4 = "nai-diffusion-4" in model  # 匹配 V4 和 V4.5

        s = self.settings
        seed = s.novelai_seed if s.novelai_seed >= 0 else random.randint(
            0, 2**32 - 1)

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
                    params["director_reference_strength_values"] = [
                        s.novelai_director_strength]
                    params["director_reference_secondary_strength_values"] = [
                        s.novelai_director_fidelity]
                    params["director_reference_information_extracted"] = [
                        s.novelai_director_info_extracted]
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
                    params["reference_information_extracted_multiple"] = [
                        s.novelai_reference_info_extracted]
                    params["reference_strength_multiple"] = [
                        s.novelai_reference_strength]
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
            session = await LLMClient.get_session()
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
            logger.error(
                f"[MemeMemPlus-NAI] API 调用异常: {traceback.format_exc()}")
        return None

    @staticmethod
    def _extract_image_from_zip(data: bytes) -> bytes | None:
        """从 NAI 返回的 zip 数据中提取图片。"""
        MAX_UNCOMPRESSED = 50 * 1024 * 1024  # 50MB 解压上限
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        continue
                    if info.file_size > MAX_UNCOMPRESSED:
                        logger.warning(f"[MemeMemPlus-NAI] zip 内文件过大({info.file_size // 1024 // 1024}MB)，跳过")
                        continue
                    return zf.read(info.filename)
            logger.warning("[MemeMemPlus-NAI] zip 中未找到图片文件")
        except zipfile.BadZipFile:
            logger.error("[MemeMemPlus-NAI] 返回数据不是有效的 zip 文件")
        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 解析响应失败: {traceback.format_exc()}")
        return None
