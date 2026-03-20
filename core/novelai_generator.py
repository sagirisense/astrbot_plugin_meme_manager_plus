"""NovelAI 角色扮演生图模块。

独立于心情表情流程，根据 Bot 对话内容 + 用户预设角色标签，
调用 LLM 补全场景/表情标签，然后调用 NovelAI API 生成图片。
支持 Vibe Transfer：在配置面板上传参考图提取角色特征作为语义引导。
"""

import base64
import collections
import hashlib
import random
import re
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
    "DO NOT generate any of the following — they are managed separately:\n"
    "- Clothing/outfit tags (dress, shirt, skirt, jacket, uniform, etc.)\n"
    "- Hosiery/legwear (thighhighs, stockings, socks, pantyhose, etc.)\n"
    "- Hairstyle tags (ponytail, braid, hair_bun, hair_down, twintails, etc.)\n"
    "- Headwear/hair accessories (hat, ribbon, hairband, bow, crown, etc.)\n"
    "- Footwear (shoes, boots, sandals, etc.)\n"
    "- Accessories (glasses, necklace, earrings, bag, etc.)\n\n"
    "Output ONLY comma-separated tags, no explanation, no base tags.\n"
    "Example: smile, looking_at_viewer, sitting, classroom, hand_on_chin"
)

SFW_TAG_ADDON = (
    "\n\n**ECCHI LIMIT RULES (HIGHEST PRIORITY, MUST OBEY):**\n"
    "- Mild skin exposure tags are ALLOWED: bare_shoulders, bare_arms, "
    "bare_legs, midriff, cleavage, navel, etc. These are fine.\n"
    "- Do NOT generate clothing/outfit tags — those are managed separately.\n"
    "- ABSOLUTELY FORBIDDEN (hard ban): "
    "nude, naked, topless, bottomless, no_bra, no_panties, nipples, pussy, penis, "
    "sex, cum, ahegao, spread_legs, bondage, tentacles, see-through_nipples, "
    "any genital exposure, any sexual action tags, any body fluid tags.\n"
    "- Rule of thumb: showing skin is OK, showing what's under the underwear is NOT.\n"
    "- Even if the dialogue is suggestive, do NOT cross into explicit territory.\n"
)

R18_TAG_ADDON = (
    "\n\n**R18 MODE — Sexual content rules:**\n"
    "Clothing/nudity state is managed by a separate system. "
    "Do NOT generate any clothing, underwear, or nudity tags.\n\n"
    "Your job: generate ONLY sexual action, expression, and atmosphere tags "
    "that match the dialogue intensity.\n\n"
    "**Intensity matching (CRITICAL — follow strictly):**\n"
    "- Casual/daily dialogue → only mild expression tags (blush, smile, etc.), "
    "NO sexual tags, NO interaction tags\n"
    "- Flirty/suggestive → light tags: blush, sweat, heavy_breathing, "
    "lip_biting, bedroom_eyes\n"
    "- Explicit sexual context → THIS is when you add interaction tags:\n"
    "  1) Character count: 1boy, 1girl, hetero (or other combo)\n"
    "  2) Action tags describing WHO does WHAT: e.g.\n"
    "     sex, vaginal, missionary, doggystyle, cowgirl_position,\n"
    "     grabbing_breasts, licking_nipple, oral, fellatio, paizuri,\n"
    "     deep_penetration, cum_in_pussy, creampie, ahegao, tongue_out\n\n"
    "Pick tags that match the ACTUAL conversation tone. "
    "Do not escalate beyond what the dialogue implies.\n\n"
    "**OUTPUT FORMAT (you MUST follow this exactly):**\n"
    "First line: comma-separated POSITIVE tags\n"
    "Second line: NEGATIVE: comma-separated tags to avoid\n"
    "Example (casual): smile, looking_at_viewer, park, sunny\n"
    "NEGATIVE: bad_anatomy, extra_limbs, blurry\n"
    "Example (explicit): 1boy, hetero, missionary, sex, blush, on_bed\n"
    "NEGATIVE: bad_anatomy, extra_limbs, blurry"
)


# R18 模式下全裸/无穿搭时的默认标签（可通过配置面板覆盖）
_DEFAULT_R18_NUDE_TAGS = "completely nude, detailed areola, visible nipples, erect nipples, arms at sides"
_DEFAULT_R18_NUDE_NEGATIVE = "covered nipples, hand covering breasts, arms covering body, censored"

# 发型固定：束发类 tag 集合（检测到时在负向加散发，反之亦然）
_HAIR_UP_KEYWORDS = {
    "ponytail", "high_ponytail", "low_ponytail", "side_ponytail",
    "twintails", "twin_braids", "braid", "braids", "side_braid", "french_braid",
    "hair_bun", "double_bun", "updo", "chignon", "hair_up",
}
_HAIR_DOWN_POSITIVE = "hair down, flowing hair"
_HAIR_DOWN_NEGATIVE = "ponytail, braid, twintails, hair_bun, updo, hair_up"
_HAIR_UP_NEGATIVE = "hair down, flowing hair"

# 正则：去除 NovelAI 权重语法 ((tag:1.2)) / {{{tag}}} / [tag] 用于去重比较
_WEIGHT_RE = re.compile(r"[(){}\[\]]")
_WEIGHT_SUFFIX_RE = re.compile(r":\d+\.?\d*$")


def _normalize_tag(tag: str) -> str:
    """去除权重语法，返回纯标签文本用于去重比较。"""
    t = _WEIGHT_RE.sub("", tag).strip()
    t = _WEIGHT_SUFFIX_RE.sub("", t).strip()
    return t.lower()


class NovelAIGenerator:
    """NovelAI 生图器：LLM 补全标签 → NAI API 生图 → 保存。"""

    _MAX_TRACKED_SESSIONS = 200

    def __init__(self, settings, context, plugin_dir: Path):
        self.settings = settings
        self.context = context
        self.output_dir = plugin_dir / "novelai"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir = self.output_dir / "generated"
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        # 参考图从配置面板上传，存放在 files/novelai_reference_image/ 目录
        self._ref_dir = plugin_dir / "files" / "novelai_reference_image"
        # 对话消息历史：按会话隔离，用于穿搭情景适配判断
        # 每条记录为 (user_text, bot_text)
        self._msg_history: dict[str, collections.deque[tuple[str, str]]] = {}
        # 上次穿搭适配输出的 tags：按会话隔离，用于保持穿着连续性
        self._last_adapted_tags: dict[str, str] = {}
        # life_scheduler 插件实例缓存
        self._life_plugin = None
        # 穿搭 tag 缓存：仅在穿搭文本变化时重新生成
        self._cached_outfit_text: str = ""
        self._cached_outfit_tags: str = ""

    def record_message(self, session_id: str, user_text: str, bot_text: str) -> None:
        """记录一条对话消息（用户输入+Bot回复），用于穿搭情景适配判断。"""
        if not session_id:
            return
        if session_id not in self._msg_history:
            # 限制跟踪的会话数，防止长期运行内存泄漏
            if len(self._msg_history) >= self._MAX_TRACKED_SESSIONS:
                oldest_key = next(iter(self._msg_history))
                del self._msg_history[oldest_key]
                self._last_adapted_tags.pop(oldest_key, None)
            self._msg_history[session_id] = collections.deque(maxlen=50)
        self._msg_history[session_id].append((user_text, bot_text))

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
            # 当天日程不存在时，往前回退最多 3 天（与 life_scheduler 逻辑一致）
            now = _dt.datetime.now()
            schedule = None
            for offset in range(4):
                s = data_mgr.get(now - _dt.timedelta(days=offset))
                if s and getattr(s, "status", "") != "failed":
                    schedule = s
                    break
            if not schedule:
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
        # 基础穿搭变了，清除所有会话的适配历史和消息历史（旧上下文已过时）
        self._last_adapted_tags.clear()
        self._msg_history.clear()
        if not raw:
            self._cached_outfit_text = ""
            self._cached_outfit_tags = ""
            logger.info("[MemeMemPlus-NAI] 穿搭已清空")
            return ""
        # 判断穿搭是否实质为空（所有衣着部位都是"无"）
        clothing_vals = [
            part.split("：", 1)[-1].strip()
            for part in raw.splitlines()
            if "：" in part and part.strip() and "风格" not in part
        ]
        is_empty = bool(clothing_vals) and all(v in ("无", "") for v in clothing_vals)
        if is_empty and self.settings.novelai_r18:
            # R18 模式下全裸：直接使用预设裸体 tags，不调 LLM
            self._cached_outfit_text = raw
            self._cached_outfit_tags = self.settings.novelai_r18_nude_tags or _DEFAULT_R18_NUDE_TAGS
            logger.info(f"[MemeMemPlus-NAI] 穿搭全为「无」+ R18 模式，使用裸体 tags")
            return self._cached_outfit_tags
        # LLM 转换穿搭描述为 NovelAI tags
        is_r18 = self.settings.novelai_r18
        try:
            r18_rules = (
                "\n5. NSFW exposure rules (R18 mode is ON):\n"
                "   - If a body part has NO clothing covering it, add explicit exposure tags:\n"
                "     - Breasts uncovered → detailed areola, visible nipples, erect nipples\n"
                "     - Lower body uncovered → nude, pussy, thighs\n"
                "     - Fully nude (all fields 無) → completely nude, detailed areola, visible nipples, arms at sides\n"
                "   - If only underwear → add the underwear tags + skin exposure tags\n"
                "   - Do NOT add nudity tags if the body part IS covered by clothing\n"
            ) if is_r18 else ""
            prompt = (
                f"Convert this character appearance description to specific NovelAI image tags "
                f"(comma-separated English tags only):\n"
                f"{raw}\n\n"
                f"Rules:\n"
                f"1. Include ALL appearance details: hairstyle, hair color, outerwear, "
                f"clothing, footwear, accessories.\n"
                f"2. Use the MOST SPECIFIC tag variant. Examples:\n"
                f"   - 'denim short_shorts' not just 'shorts'\n"
                f"   - 'black pleated_skirt' not just 'skirt'\n"
                f"   - 'white oversized_hoodie' not just 'hoodie'\n"
                f"   - 'low_ponytail' not just 'ponytail'\n"
                f"3. Add body exposure tags implied by the clothing:\n"
                f"   - short sleeves / sleeveless → bare_arms\n"
                f"   - shorts / short skirt → bare_legs, thighs\n"
                f"   - crop top / midriff → bare_shoulders, navel\n"
                f"   - sandals / barefoot → bare_feet\n"
                f"4. Describe clothing style details (material, pattern, fit):\n"
                f"   - e.g. 'striped long_sleeves', 'plaid_shirt', 'ribbed_sweater', "
                f"'lace_trim', 'denim_jacket', 'oversized_t-shirt'\n"
                f"5. HAIRSTYLE (MANDATORY): Always include hairstyle tags.\n"
                f"   - If the description mentions a hairstyle → use that (e.g. ponytail, braid, hair_bun)\n"
                f"   - If NO hairstyle is mentioned → default to: hair down, flowing hair\n"
                f"6. HAIR ACCESSORIES: If the description mentions hair accessories (ribbon, hairpin, bow, etc.),\n"
                f"   always specify their position (e.g. hair_ribbon_on_left, hairpin_on_right, hair_bow_center).\n"
                f"   This ensures consistent placement across multiple images.\n"
                f"{r18_rules}"
                f"Output ONLY comma-separated tags. No explanation."
            )
            result = await LLMClient.call(
                cfg, prompt,
                system_msg="You are a NovelAI tag expert. Output ONLY comma-separated English tags. Be highly specific about clothing details and implied body exposure.",
                max_tokens=200,
                timeout=self.settings.llm_timeout,
            )
            # 多行输出合并为逗号分隔的单行
            if result:
                lines = [l.strip().rstrip(",").strip() for l in result.splitlines() if l.strip()]
                tags = ", ".join(lines).rstrip(",").strip()
            else:
                tags = ""
            self._cached_outfit_text = raw  # 成功后才更新缓存 key，失败时下次可重试
            self._cached_outfit_tags = tags
            logger.info(f"[MemeMemPlus-NAI] 穿搭 tag 已更新: '{raw[:30]}...' → '{tags}'")
        except Exception as e:
            logger.warning(f"[MemeMemPlus-NAI] 穿搭 tag 转换失败，下次将重试: {e}")
            # 不更新 _cached_outfit_text，下次调用时 raw != cached 会重试
        return self._cached_outfit_tags

    async def _adapt_outfit_tags(self, cfg, session_id: str, current_tags: str) -> str:
        """根据对话历史判断是否需要临时修改穿搭 tag。

        保存每次适配结果到 _last_adapted_tags（按会话），下次适配时传给 LLM
        以保持穿着连续性（颜色、款式等细节不丢失）。不修改 _cached_outfit_tags。
        """
        history = self._msg_history.get(session_id)
        if not history:
            return current_tags
        # 取最近 N 条对话消息
        n = self.settings.novelai_outfit_history
        recent = list(history)[-n:]
        # 构造对话上下文
        conv_lines = []
        for user_msg, bot_msg in recent:
            if user_msg:
                conv_lines.append(f"用户: {user_msg[:100]}")
            if bot_msg:
                conv_lines.append(f"Bot: {bot_msg[:100]}")
        conversation = "\n".join(conv_lines)

        # 上次适配输出的 tags（可能与 base outfit 不同）
        last_adapted = self._last_adapted_tags.get(session_id, "")

        prompt = (
            f"Base outfit tags (daily default): {current_tags}\n"
        )
        if last_adapted and last_adapted != current_tags:
            prompt += f"Last generated outfit tags (from previous image): {last_adapted}\n"
        prompt += (
            f"\nRecent conversation ({len(recent)} messages):\n{conversation}\n\n"
            f"Judge: based on the conversation, what should the character be wearing RIGHT NOW?\n\n"
            f"CONTINUITY RULES (HIGHEST PRIORITY):\n"
            f"- If last generated tags exist, treat them as the character's CURRENT state\n"
            f"- Preserve specific details from the last output: colors (pink_panties stays pink),\n"
            f"  materials (lace, cotton, silk), patterns (striped, polka_dot), styles (high-waist, low-rise)\n"
            f"- Only change items that the conversation explicitly changes\n"
            f"- Example: if last output had 'pink_lace_bra, pink_lace_panties' and user says '脱掉上衣',\n"
            f"  keep 'pink_lace_panties' but remove the bra-related tags\n\n"
            f"DETAIL RULES:\n"
            f"- Be MAXIMALLY specific about every clothing item: include color, material, style\n"
            f"- BAD: underwear, bra, panties (too vague)\n"
            f"- GOOD: pink_lace_bra, white_cotton_panties, black_silk_nightgown, light_blue_striped_pajamas\n"
            f"- Include body exposure tags implied by the outfit state\n"
            f"- NEW ITEM COMPLETION: When the conversation adds a new item without full details,\n"
            f"  you MUST invent reasonable specifics (color, material, shape) using minimal tags.\n"
            f"  Example: '戴个项链' → silver_pendant_necklace, thin_chain\n"
            f"  Example: '换双袜子' → white_knee_high_socks\n"
            f"  Example: '围个围巾' → red_knitted_scarf\n"
            f"  This ensures the item looks consistent in future images.\n\n"
            f"HAIRSTYLE & ACCESSORIES CONTINUITY:\n"
            f"- Always preserve the current hairstyle unless the conversation explicitly changes it\n"
            f"- Hair accessories (ribbon, hairpin, bow, etc.) must keep their position "
            f"(e.g. hair_ribbon_on_left stays on_left). Do NOT move or remove them unless requested\n\n"
            f"SCENE HINTS:\n"
            f"- 洗澡/泡澡/淋浴 → towel, bare_shoulders, wet_hair, or nude depending on context\n"
            f"- 游泳 → swimsuit/bikini with colors\n"
            f"- 睡觉 → pajamas/nightgown with specific style\n"
            f"- 换衣/脱衣 → modify items accordingly, keep unchanged items with full detail\n\n"
            f"If outfit should CHANGE: output MODIFIED comma-separated tags with FULL details\n"
            f"If NO change needed: output ONLY the word KEEP\n"
            f"Output ONLY tags or KEEP. No explanation."
        )
        logger.debug(
            f"[MemeMemPlus-NAI] 穿搭适配判断: {len(recent)} 条历史, "
            f"base='{current_tags[:40]}', last='{last_adapted[:40] if last_adapted else '无'}'"
        )
        try:
            result = await LLMClient.call(
                cfg, prompt,
                system_msg=(
                    "You judge if a conversation requires outfit changes for an anime character. "
                    "Maintain continuity with previous outfit state. "
                    "Be VERY specific about clothing details (color, material, style). "
                    "Output modified tags or KEEP."
                ),
                max_tokens=200,
                timeout=self.settings.llm_timeout,
            )
            logger.debug(f"[MemeMemPlus-NAI] 穿搭适配 LLM 输出: '{result}'")
            if not result or result.strip().upper() == "KEEP":
                # KEEP = 维持当前状态（上次适配结果 > 基础穿搭）
                return self._last_adapted_tags.get(session_id, current_tags)
            lines = [l.strip().rstrip(",").strip() for l in result.splitlines() if l.strip()]
            adapted = ", ".join(lines).rstrip(",").strip()
            if adapted:
                self._last_adapted_tags[session_id] = adapted
                # 适配成功：旧对话已被消化，清空该会话历史避免重复判断
                history = self._msg_history.get(session_id)
                if history is not None:
                    history.clear()
                logger.info(f"[MemeMemPlus-NAI] 穿搭情景适配: '{current_tags[:30]}...' → '{adapted[:50]}...'")
                return adapted
            return current_tags
        except Exception as e:
            logger.debug(f"[MemeMemPlus-NAI] 穿搭适配判断失败: {e}")
            return self._last_adapted_tags.get(session_id, current_tags)

    def clear_caches(self) -> None:
        """清空对话历史、穿搭缓存和适配历史。外部可在 /reset 等场景调用。"""
        self._msg_history.clear()
        self._last_adapted_tags.clear()
        self._cached_outfit_text = ""
        self._cached_outfit_tags = ""
        logger.info("[MemeMemPlus-NAI] 对话历史和穿搭缓存已清空")

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
        max_size = self.settings.novelai_max_cache
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
        def _safe_mtime(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0
        files = sorted(all_files, key=_safe_mtime)
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
        llm_enabled = self.settings.novelai_llm_enabled

        if llm_enabled:
            # LLM 模式：用 LLM 根据对话内容补全标签
            cfg = load_mood_provider(self.context, self.settings, self.settings.novelai_llm_provider_id)
            if not cfg.valid:
                logger.warning("[MemeMemPlus-NAI] 未找到 LLM 提供商配置，无法补全标签")
                return None, None
            extra_tags, extra_negative = await self._generate_tags(cfg, bot_reply, base_tags)
            if not extra_tags:
                logger.warning("[MemeMemPlus-NAI] LLM 标签补全失败，使用基础标签生图")
                full_tags = base_tags
            else:
                # 去重：LLM 可能重复输出 base_tags 中已有的标签（去除权重语法后比较）
                base_set = {_normalize_tag(t) for t in base_tags.split(",") if t.strip()}
                deduped = ", ".join(
                    t.strip() for t in extra_tags.split(",")
                    if t.strip() and _normalize_tag(t) not in base_set
                )
                full_tags = f"{base_tags}, {deduped}" if deduped else base_tags
        else:
            # 纯标签模式：不调用 LLM，直接用 base_tags
            logger.info("[MemeMemPlus-NAI] LLM 已关闭，使用基础标签直接生图")
            full_tags = base_tags

        # 追加用户自定义标签（仅非 R18 模式）
        if not self.settings.novelai_r18:
            custom_tags = self.settings.novelai_custom_tags.strip()
            if custom_tags:
                full_tags = f"{full_tags}, {custom_tags}"
        # R18 模式：追加专用自定义标签
        if self.settings.novelai_r18:
            r18_custom = self.settings.novelai_r18_custom_tags.strip()
            if r18_custom:
                full_tags = f"{full_tags}, {r18_custom}"

        # 穿搭 tags：直接拼接到末尾，用 (tag:weight) 控制权重
        outfit_negative: str | None = None
        if self.settings.novelai_use_outfit:
            if not llm_enabled:
                # 穿搭需要 LLM 转换，非 LLM 模式时跳过
                logger.debug("[MemeMemPlus-NAI] LLM 已关闭，跳过穿搭 tag 注入")
                outfit_tags = ""
            else:
                outfit_tags = await self._refresh_outfit_tags(cfg)
                # 情景适配：根据对话历史临时修改穿搭（不修改缓存）
                adapt_on = self.settings.novelai_outfit_adapt
                if outfit_tags and adapt_on:
                    outfit_tags = await self._adapt_outfit_tags(cfg, session_id, outfit_tags)
                elif adapt_on and not outfit_tags:
                    logger.debug("[MemeMemPlus-NAI] 穿搭适配已开启但无穿搭 tag，跳过")
            # 发型固定：检测 outfit_tags 中的发型，生成对应 negative 防止冲突
            if outfit_tags:
                outfit_tag_set = {t.strip().lower().replace(" ", "_") for t in outfit_tags.split(",")}
                has_hair_up = bool(outfit_tag_set & _HAIR_UP_KEYWORDS)
                if has_hair_up:
                    # 束发类发型 → negative 加散发
                    hair_neg = _HAIR_UP_NEGATIVE
                else:
                    # 散发或无发型 → 确保有散发正向 tag，negative 加束发
                    if "hair_down" not in outfit_tag_set and "flowing_hair" not in outfit_tag_set:
                        outfit_tags = f"{outfit_tags}, {_HAIR_DOWN_POSITIVE}"
                    hair_neg = _HAIR_DOWN_NEGATIVE
                outfit_negative = f"{outfit_negative}, {hair_neg}" if outfit_negative else hair_neg
            # R18 裸体检测：穿搭 tags 包含裸体关键词时追加 negative
            if outfit_tags and self.settings.novelai_r18:
                nude_keywords = {"completely_nude", "completely nude", "naked", "nude"}
                outfit_lower = outfit_tags.lower()
                if any(kw in outfit_lower for kw in nude_keywords):
                    nude_neg = self.settings.novelai_r18_nude_negative or _DEFAULT_R18_NUDE_NEGATIVE
                    outfit_negative = f"{outfit_negative}, {nude_neg}" if outfit_negative else nude_neg
                    logger.debug("[MemeMemPlus-NAI] 裸体穿搭检测，追加 nude negative tags")
            if outfit_tags:
                ow = self.settings.novelai_outfit_weight
                if abs(ow - 1.0) < 0.01:
                    # 权重为 1.0 时不加权重标记
                    full_tags = f"{full_tags}, {outfit_tags}"
                else:
                    weighted = ", ".join(f"({t.strip()}:{ow})" for t in outfit_tags.split(",") if t.strip())
                    full_tags = f"{full_tags}, {weighted}"

        # 合并穿搭 negative tags
        if outfit_negative:
            extra_negative = f"{extra_negative}, {outfit_negative}" if extra_negative else outfit_negative

        logger.info(f"[MemeMemPlus-NAI] 最终正向标签: {full_tags}")
        final_negative = self.settings.novelai_negative_prompt
        if extra_negative:
            final_negative = f"{final_negative}, {extra_negative}" if final_negative else extra_negative
        logger.info(f"[MemeMemPlus-NAI] 最终负向标签: {final_negative}")

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
        logger.info(f"[MemeMemPlus-NAI] /ni 负向标签: {self.settings.novelai_negative_prompt}")
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
        self, cfg: LLMApiConfig, bot_reply: str, base_tags: str,
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
        is_r18 = self.settings.novelai_r18
        # R18 模式：追加 NSFW 标签生成指令（含 NEGATIVE 行输出格式）
        if is_r18:
            prompt += R18_TAG_ADDON
        elif self.settings.novelai_safe_mode:
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
            # NEGATIVE: 行仅在 R18 模式下解析（只有 R18_TAG_ADDON 要求此格式）
            lower = stripped.lower()
            if is_r18 and lower.startswith("negative:"):
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
