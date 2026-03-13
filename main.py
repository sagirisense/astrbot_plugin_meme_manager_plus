import asyncio
import random
import traceback
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import AstrBotConfig

from .config.settings import ConfigLoader
from .core.library_manager import LibraryManager
from .core.mood_analyzer import MoodAnalyzer
from .core.image_manager import MoodImageManager
from .core.auto_updater import AutoUpdater
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

        # 初始化数据目录和图库（放在插件目录下，与借鉴项目保持一致）
        self.plugin_dir = Path(__file__).parent
        self.library_dir = self.plugin_dir / "memes"

        self.library_mgr = LibraryManager(self.library_dir)
        self.library_mgr.initialize()

        # 初始化核心组件
        self.mood_analyzer = MoodAnalyzer(context, self.settings)
        self.image_mgr = MoodImageManager(self.settings, context)
        self.cooldown = CooldownManager(
            self.settings.cooldown_seconds, self.settings.per_group
        )

        # 自动图库更新
        self.auto_updater = AutoUpdater(self.settings, context, self.library_mgr)
        if self.settings.auto_update_enabled:
            self.auto_updater.start()

        logger.info(
            f"[MemeMemPlus] 插件初始化完成, "
            f"启用={self.settings.enabled}, "
            f"自动更新={'开' if self.settings.auto_update_enabled else '关'}, "
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
            logger.info(f"[MemeMemPlus] 冷却中，跳过 (session={session_id})")
            return

        # 概率判定移到情绪分析之后，先启动后台任务
        self.cooldown.record(session_id, group_id)
        asyncio.create_task(
            self._generate_and_send(event, response.completion_text)
        )

    async def _generate_and_send(
        self, event: AstrMessageEvent, text: str
    ) -> None:
        """后台任务：情绪分析 + 表达欲望判定 → 可选生图 → 抽图发送。"""
        try:
            available_moods = self.library_mgr.get_all_moods()
            if not available_moods:
                logger.warning("[MemeMemPlus] 无可用心情标签")
                return

            # 情绪分析（同时获取表达欲望评分）
            score, mood = await self.mood_analyzer.analyze(text, available_moods)
            if not mood:
                logger.info(f"[MemeMemPlus] 情绪分析未匹配: score={score:.2f}")
                return

            # 第一级：触发概率（表达欲望）
            threshold = 1.0 - self.settings.default_probability / 100.0
            if score <= threshold:
                logger.info(
                    f"[MemeMemPlus] 表达欲望不足: score={score:.2f} <= {threshold:.2f}, mood={mood}"
                )
                return

            logger.info(
                f"[MemeMemPlus] 触发表情: score={score:.2f} > {threshold:.2f}, mood={mood}"
            )

            # 先从该心情目录随机抽一张发送（不被生图阻塞）
            all_images = self.library_mgr.get_all_references(mood)
            if not all_images:
                logger.info(f"[MemeMemPlus] {mood} 目录为空，不发送")
            else:
                picked = random.choice(all_images)
                image_bytes = picked.read_bytes()
                logger.info(f"[MemeMemPlus] 随机抽取: {picked.name}, mood={mood}")
                await self._send_image(event, image_bytes)

            # 第二级：LLM 生图概率（独立判定，发送后再生图入库）
            if (
                self.settings.llm_generation_enabled
                and random.randint(1, 100) <= self.settings.llm_generation_probability
            ):
                ref_paths = self.library_mgr.get_all_references(mood)
                max_images = self.settings.max_images_per_mood

                if max_images <= 0 or len(ref_paths) < max_images:
                    sample_refs = ref_paths
                    if len(sample_refs) > 3:
                        sample_refs = random.sample(sample_refs, 3)

                    logger.info(
                        f"[MemeMemPlus] LLM生图命中: mood={mood}, "
                        f"mode={'图生图' if sample_refs else '文生图'}"
                    )

                    gen_bytes = await self.image_mgr.generate(mood, sample_refs or None)
                    if gen_bytes:
                        self._save_generated_image(mood, gen_bytes)
                else:
                    logger.info(
                        f"[MemeMemPlus] 图库已满({len(ref_paths)}>={max_images})，跳过生图"
                    )
            else:
                logger.info(f"[MemeMemPlus] LLM生图未命中或已关闭, mood={mood}")

        except Exception:
            logger.error(f"[MemeMemPlus] 后台任务异常: {traceback.format_exc()}")

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
            f"触发概率: {self.settings.default_probability}%",
            f"大模型生图: {'开启' if self.settings.llm_generation_enabled else '关闭'}",
            f"生图概率: {self.settings.llm_generation_probability}%",
            f"每心情上限: {self.settings.max_images_per_mood}",
            f"图库: {len(stats)} 个心情, {non_empty} 个有参考图, 共 {total} 张",
            "",
            f"--- 自动搜图 ---",
            f"状态: {'运行中' if self.auto_updater.running else '已关闭'}",
            f"间隔: {self.settings.auto_update_interval_hours}h",
            f"标签: {self.settings.auto_update_search_tags}",
            f"来源: {self.settings.auto_update_source}",
            f"每次: {self.settings.auto_update_images_per_cycle} 张",
            "",
            "--- 各心情统计 ---",
        ]

        for mood, count in sorted(stats.items()):
            max_img = self.settings.max_images_per_mood
            if count == 0:
                mode = "空（不发送）"
            elif max_img > 0 and count >= max_img:
                mode = "已满，仅抽取"
            else:
                mode = "抽取" + ("＋可生图" if self.settings.llm_generation_enabled else "")
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

    @filter.command("自动搜图开启")
    async def enable_auto_update(self, event: AstrMessageEvent):
        """开启自动图库更新。"""
        if self.auto_updater.running:
            yield event.plain_result("自动搜图已经在运行中")
            return
        self.auto_updater.start()
        yield event.plain_result(
            f"自动搜图已开启\n"
            f"间隔: {self.settings.auto_update_interval_hours}h\n"
            f"标签: {self.settings.auto_update_search_tags}\n"
            f"来源: {self.settings.auto_update_source}\n"
            f"每次: {self.settings.auto_update_images_per_cycle} 张"
        )

    @filter.command("自动搜图关闭")
    async def disable_auto_update(self, event: AstrMessageEvent):
        """关闭自动图库更新。"""
        if not self.auto_updater.running:
            yield event.plain_result("自动搜图未在运行")
            return
        self.auto_updater.stop()
        yield event.plain_result("自动搜图已关闭")

    @filter.command("自动搜图立即执行")
    async def run_auto_update_now(self, event: AstrMessageEvent):
        """立即执行一次自动搜图。"""
        yield event.plain_result("开始搜图，请稍候...")
        asyncio.create_task(self.auto_updater._run_once())

    @filter.command("搜图")
    async def manual_search(self, event: AstrMessageEvent, count: int = 5):
        """手动搜图：搜图 N，使用配置的标签搜索 N 张图片并分类入库。"""
        count = min(max(1, count), 50)
        yield event.plain_result(
            f"开始搜索 {count} 张图片...\n"
            f"标签: {self.settings.auto_update_search_tags}\n"
            f"来源: {self.settings.auto_update_source}"
        )

        async def _do_search():
            saved = await self.auto_updater._run_once(limit_override=count)
            try:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(f"搜图完成，入库 {saved} 张"),
                )
            except Exception:
                logger.error(f"[MemeMemPlus] 发送搜图结果失败: {traceback.format_exc()}")

        asyncio.create_task(_do_search())
