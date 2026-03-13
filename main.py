import asyncio
import random
import traceback

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .config.settings import ConfigLoader
from .core.library_manager import LibraryManager
from .core.mood_analyzer import MoodAnalyzer
from .core.image_manager import MoodImageManager
from .utils.cooldown_manager import CooldownManager


@register(
    "astrbot_plugin_meme_manager_plus",
    "LoveRoxy",
    "AI 心情表情管理器 - 自动检测回复情绪，通过 Gemini API 生成灵活多变的表情图片",
    "1.0.0",
    "",
)
class MoodMemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 加载配置
        cfg_loader = ConfigLoader(config)
        self.settings = cfg_loader.load()

        # 初始化数据目录和图库（使用框架数据目录，不污染源码目录）
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_meme_manager_plus")
        self.library_dir = self.data_dir / "memes"

        self.library_mgr = LibraryManager(self.library_dir)
        self.library_mgr.initialize()

        # 初始化核心组件
        self.mood_analyzer = MoodAnalyzer(context, self.settings)
        self.image_mgr = MoodImageManager(self.settings, context)
        self.cooldown = CooldownManager(
            self.settings.cooldown_seconds, self.settings.per_group
        )

        logger.info(
            f"[MemeMemPlus] 插件初始化完成, "
            f"启用={self.settings.enabled}, "
            f"图库心情数={len(self.library_mgr.get_all_moods())}"
        )

    @filter.on_llm_response()
    async def handle_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """LLM 回复后：轻量判断，命中则后台生图，不阻塞文本回复。"""
        if not self.settings.enabled:
            return

        if not response or not response.completion_text:
            return

        session_id = event.session_id
        group_id = str(getattr(event.message_obj, "group_id", "")) or None

        if not self.cooldown.can_trigger(session_id, group_id):
            return

        # 概率判定移到情绪分析之后，先启动后台任务
        self.cooldown.record(session_id, group_id)
        asyncio.create_task(
            self._generate_and_send(event, response.completion_text)
        )

    async def _generate_and_send(
        self, event: AstrMessageEvent, text: str
    ) -> None:
        """后台任务：情绪分析 + 表达欲望判定 → 生图/抽图 → 发送。不阻塞主流程。"""
        try:
            # 情绪分析（同时获取表达欲望评分）
            available_moods = self.library_mgr.get_all_moods()
            if not available_moods:
                return

            score, mood = await self.mood_analyzer.analyze(text, available_moods)
            if not mood:
                return

            # 表达欲望判定：score > 1 - p 时触发
            # p = default_probability / 100，例如 p=0.2 时阈值为 0.8
            threshold = 1.0 - self.settings.default_probability / 100.0
            if score <= threshold:
                logger.debug(
                    f"[MemeMemPlus] 表达欲望不足: score={score:.2f} <= threshold={threshold:.2f}, "
                    f"mood={mood}"
                )
                return

            logger.debug(
                f"[MemeMemPlus] 触发生图: score={score:.2f} > threshold={threshold:.2f}, "
                f"mood={mood}"
            )

            # 检查是否已达上限
            ref_paths = self.library_mgr.get_all_references(mood)
            max_images = self.settings.max_images_per_mood

            if max_images > 0 and len(ref_paths) >= max_images:
                # 已达上限，随机抽取
                picked = random.choice(ref_paths)
                image_bytes = picked.read_bytes()
                logger.debug(
                    f"[MemeMemPlus] 图库已满({len(ref_paths)}>={max_images})，"
                    f"随机抽取: {picked.name}"
                )
            else:
                # 生成新图
                sample_refs = ref_paths
                if len(sample_refs) > 3:
                    sample_refs = random.sample(sample_refs, 3)

                logger.debug(
                    f"[MemeMemPlus] 开始生图: mood={mood}, "
                    f"mode={'图生图' if sample_refs else '文生图'}, refs={len(sample_refs)}"
                )

                image_bytes = await self.image_mgr.generate(mood, sample_refs or None)
                if not image_bytes:
                    return

                # 保存到图库
                self._save_generated_image(mood, image_bytes)

            # 直接发送图片
            await self._send_image(event, image_bytes)

        except Exception:
            logger.error(f"[MemeMemPlus] 后台生图任务异常: {traceback.format_exc()}")

    async def _send_image(self, event: AstrMessageEvent, image_bytes: bytes) -> None:
        """发送图片到对应会话。"""
        try:
            if event.get_platform_name() == "gewechat":
                await event.send(
                    MessageChain([Image.fromBytes(image_bytes)])
                )
            else:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Image.fromBytes(image_bytes)]),
                )
            logger.info("[MemeMemPlus] 表情图片已发送")
        except Exception:
            logger.error(f"[MemeMemPlus] 发送图片失败: {traceback.format_exc()}")

    def _save_generated_image(self, mood: str, image_bytes: bytes) -> None:
        """将生成的图片保存到对应心情目录，并刷新缓存。"""
        import hashlib
        mood_dir = self.library_dir / mood
        mood_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.md5(image_bytes).hexdigest()[:12]
        save_path = mood_dir / f"gen_{name}.png"
        try:
            save_path.write_bytes(image_bytes)
            self.library_mgr.refresh()
            logger.debug(f"[MemeMemPlus] 图片已保存: {save_path.name}")
        except Exception:
            logger.warning(f"[MemeMemPlus] 保存图片失败: {traceback.format_exc()}")

    @filter.command("心情表情状态")
    async def show_status(self, event: AstrMessageEvent):
        """查看插件状态和图库统计。"""
        stats = self.library_mgr.get_stats()
        total = sum(stats.values())
        non_empty = sum(1 for v in stats.values() if v > 0)

        lines = [
            "=== AI 心情表情管理器 ===",
            f"状态: {'启用' if self.settings.enabled else '禁用'}",
            f"模型: {self.settings.model}",
            f"冷却: {self.settings.cooldown_seconds}秒",
            f"默认概率: {self.settings.default_probability}%",
            f"每心情上限: {self.settings.max_images_per_mood}",
            f"图库: {len(stats)} 个心情, {non_empty} 个有参考图, 共 {total} 张",
            "",
            "--- 各心情统计 ---",
        ]

        for mood, count in sorted(stats.items()):
            max_img = self.settings.max_images_per_mood
            if max_img > 0 and count >= max_img:
                mode = "已满，随机抽取"
            elif count > 0:
                mode = "图生图"
            else:
                mode = "文生图"
            lines.append(f"  {mood}: {count}张 ({mode})")

        yield event.plain_result("\n".join(lines))

    @filter.command("心情表情刷新")
    async def refresh_library(self, event: AstrMessageEvent):
        """重新扫描图库目录。"""
        self.library_mgr.refresh()
        stats = self.library_mgr.get_stats()
        total = sum(stats.values())
        yield event.plain_result(
            f"图库已刷新: {len(stats)} 个心情, 共 {total} 张参考图"
        )
