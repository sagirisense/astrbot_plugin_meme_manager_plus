import asyncio
import collections
import datetime
import hashlib
import io
import json
import random
import shutil
import traceback
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.api import AstrBotConfig

from .config.settings import ConfigLoader
from .core.library_manager import LibraryManager
from .core.mood_analyzer import MoodAnalyzer
from .core.image_manager import MoodImageManager
from .core.auto_updater import AutoUpdater
from .core.novelai_generator import NovelAIGenerator
from .utils.cooldown_manager import CooldownManager
from .utils.provider_helper import load_mood_provider


def _format_search_result(result: dict, prefix: str = "搜图") -> str:
    """将 auto_updater._run_once() 返回的 dict 格式化为用户消息。"""
    if "skipped" in result:
        reason = result["skipped"]
        if reason == "busy":
            return "已有搜图任务在执行中，请等待完成后再试"
        if reason == "library_full":
            return f"图库已达上限（{result.get('total', '?')} 张），跳过搜图"
        if reason == "no_moods":
            return "无可用心情分类目录，请先创建心情文件夹"
        return f"{prefix}被跳过: {reason}"

    saved = result.get("saved", 0)
    searched = result.get("searched", 0)
    if searched == 0:
        return f"{prefix}完成，未找到新图片"

    lines = [f"{prefix}完成，入库 {saved}/{searched} 张"]
    dl_fail = result.get("dl_fail", 0)
    filtered = result.get("filtered", 0)
    mood_fail = result.get("mood_fail", 0)
    details = []
    if dl_fail:
        details.append(f"下载失败 {dl_fail}")
    if filtered:
        details.append(f"筛选拒绝 {filtered}")
    if mood_fail:
        details.append(f"分类失败 {mood_fail}")
    if details:
        lines.append("（" + "，".join(details) + "）")
    total = result.get("total")
    if total is not None:
        lines.append(f"图库总计: {total} 张")
    return "\n".join(lines)


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

        # 删图功能：trash 目录
        self.trash_dir = self.plugin_dir / "trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        # NovelAI 生图模式（独立）
        self.novelai_gen = NovelAIGenerator(self.settings, context, self.plugin_dir)
        self.novelai_cooldown = CooldownManager(
            self.settings.novelai_cooldown_seconds, self.settings.per_group
        )

        # Tag 预设存储目录
        self._tag_preset_dir = self.plugin_dir / "novelai" / "tag_presets"
        self._tag_preset_dir.mkdir(parents=True, exist_ok=True)

        # 记录每个会话最近发送的图片路径和消息ID，用于 /删图
        # 格式: {unified_msg_origin: (file_path, message_id_or_None)}
        # OrderedDict 保证 FIFO 淘汰顺序
        self._last_sent: collections.OrderedDict[str, tuple[Path, str | int | None]] = collections.OrderedDict()
        self._MAX_LAST_SENT = 200  # 防止内存泄漏

        # 保存后台任务引用，防止被 GC 回收导致异常丢失
        self._bg_tasks: set[asyncio.Task] = set()

        # 启动穿搭自动同步任务（检测 life_scheduler 更新）
        if self.settings.novelai_use_outfit:
            self._launch_bg_task(self._outfit_sync_loop())

        logger.info(
            f"[MemeMemPlus] 插件初始化完成, "
            f"启用={self.settings.enabled}, "
            f"自动更新={'开' if self.settings.auto_update_enabled else '关'}, "
            f"图库心情数={len(self.library_mgr.get_all_moods())}"
        )

    def _record_last_sent(self, origin: str, path: Path, msg_id: str | int | None) -> None:
        """记录最近发送的图片，超限时 FIFO 淘汰。"""
        self._last_sent[origin] = (path, msg_id)
        while len(self._last_sent) > self._MAX_LAST_SENT:
            self._last_sent.popitem(last=False)

    def _launch_bg_task(self, coro) -> asyncio.Task:
        """创建后台任务并持有引用，任务完成后自动移除。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"[MemeMemPlus] 后台任务未捕获异常: {type(exc).__name__}: {exc}")

        task.add_done_callback(_on_done)
        return task

    async def _outfit_sync_loop(self):
        """后台循环：每分钟检测 life_scheduler 穿搭更新，自动刷新缓存。"""
        await asyncio.sleep(30)  # 首次延迟 30 秒，避免初始化冲突
        while True:
            try:
                if self.settings.novelai_use_outfit and self.settings.novelai_llm_enabled:
                    raw_outfit = self.novelai_gen._get_raw_outfit()
                    if raw_outfit and raw_outfit != self.novelai_gen._cached_outfit_text:
                        from .utils.provider_helper import load_mood_provider
                        cfg = load_mood_provider(self.context, self.settings, self.settings.novelai_llm_provider_id)
                        if cfg.valid:
                            logger.info(f"[MemeMemPlus] 检测到穿搭变化，自动刷新缓存")
                            await self.novelai_gen._refresh_outfit_tags(cfg)
                            logger.info(f"[MemeMemPlus] 穿搭缓存已更新: {self.novelai_gen._cached_outfit_tags[:60]}")
            except Exception as e:
                logger.debug(f"[MemeMemPlus] 穿搭同步异常: {e}")
            await asyncio.sleep(60)  # 每分钟检查一次

    async def _on_unload(self):
        """插件卸载时清理资源。"""
        self.auto_updater.stop()
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[MemeMemPlus] 后台任务未在 5 秒内结束，强制清理")
        self._bg_tasks.clear()
        from .utils.llm_client import LLMClient
        await LLMClient.close()

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
        group_id = getattr(event.message_obj, "group_id", None)
        group_id = str(group_id) if group_id else None

        # NovelAI 模式：开启时走独立流程，跳过心情表情
        if self.settings.novelai_enabled:
            # 无论是否触发生图，都记录对话消息（穿搭情景适配需要完整历史）
            self.novelai_gen.record_message(
                session_id, event.message_str, response.completion_text
            )
            if not self.novelai_cooldown.can_trigger(session_id, group_id):
                logger.info(f"[MemeMemPlus-NAI] 冷却中，跳过 (session={session_id})")
                return
            if random.randint(1, 100) > self.settings.novelai_probability:
                logger.info("[MemeMemPlus-NAI] 概率未命中，跳过")
                return
            # 先记录冷却防止并发重复触发
            self.novelai_cooldown.record(session_id, group_id)
            self._launch_bg_task(
                self._novelai_generate_and_send(event, response.completion_text, session_id, group_id)
            )
            return

        if not self.cooldown.can_trigger(session_id, group_id):
            logger.info(f"[MemeMemPlus] 冷却中，跳过 (session={session_id})")
            return

        # 先记录冷却防止并发重复触发
        self.cooldown.record(session_id, group_id)
        self._launch_bg_task(
            self._generate_and_send(event, response.completion_text, session_id, group_id)
        )

    async def _generate_and_send(
        self, event: AstrMessageEvent, text: str,
        session_id: str, group_id: str | None,
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

            # 第一级：表达欲望门槛
            threshold = self.settings.expression_threshold
            if score < threshold:
                logger.info(
                    f"[MemeMemPlus] 表达欲望不足: score={score:.2f} < {threshold:.2f}, mood={mood}"
                )
                return

            logger.info(
                f"[MemeMemPlus] 触发表情: score={score:.2f} >= {threshold:.2f}, mood={mood}"
            )

            # 先从该心情目录随机抽一张发送（不被生图阻塞）
            all_images = self.library_mgr.get_all_references(mood)
            if not all_images:
                logger.info(f"[MemeMemPlus] {mood} 目录为空，不发送")
            else:
                picked = random.choice(all_images)
                try:
                    image_bytes = picked.read_bytes()
                except OSError:
                    logger.warning(f"[MemeMemPlus] 抽取的图片读取失败（可能已被删除）: {picked.name}")
                    image_bytes = None
                if image_bytes:
                    logger.info(f"[MemeMemPlus] 随机抽取: {picked.name}, mood={mood}")
                    sent_msg_id = await self._send_image(event, image_bytes)
                    self._record_last_sent(event.unified_msg_origin, picked, sent_msg_id)

            # 第二级：LLM 生图概率（独立判定，发送后再生图入库）
            if not (
                self.settings.llm_generation_enabled
                and random.randint(1, 100) <= self.settings.llm_generation_probability
            ):
                logger.info(f"[MemeMemPlus] LLM生图未命中或已关闭, mood={mood}")
                return

            # 图库总上限检查
            max_lib = self.settings.max_library_size
            if max_lib > 0:
                total = sum(self.library_mgr.get_stats().values())
                if total >= max_lib:
                    logger.info(f"[MemeMemPlus] 图库已达上限({total}>={max_lib})，跳过生图")
                    return

            ref_paths = self.library_mgr.get_all_references(mood)
            sample_refs = random.sample(ref_paths, 3) if len(ref_paths) > 3 else ref_paths
            logger.info(f"[MemeMemPlus] LLM生图命中: mood={mood}, mode={'图生图' if sample_refs else '文生图'}")
            gen_bytes = await self.image_mgr.generate(mood, sample_refs or None)
            if gen_bytes:
                self._save_generated_image(mood, gen_bytes)

        except Exception:
            logger.error(f"[MemeMemPlus] 后台任务异常: {traceback.format_exc()}")

    async def _novelai_generate_and_send(
        self, event: AstrMessageEvent, text: str,
        session_id: str, group_id: str | None,
    ) -> None:
        """NovelAI 后台任务：LLM 补全标签 → NAI API 生图 → 发送。"""
        try:
            logger.info("[MemeMemPlus-NAI] 开始生图流程")
            image_bytes, save_path = await self.novelai_gen.run(
                text, session_id=session_id
            )
            if not image_bytes:
                logger.warning("[MemeMemPlus-NAI] 生图失败")
                return

            sent_msg_id = await self._send_image(event, image_bytes, sticker=self.settings.novelai_sticker_mode)
            if save_path:
                self._record_last_sent(event.unified_msg_origin, Path(save_path), sent_msg_id)
            logger.info(f"[MemeMemPlus-NAI] 生图完成并发送, saved={save_path}")

        except Exception:
            logger.error(f"[MemeMemPlus-NAI] 后台任务异常: {traceback.format_exc()}")

    def _to_sticker(self, image_bytes: bytes, size: int = 200) -> bytes:
        """将图片等比缩放到正方形内，透明填充空白区域，输出 GIF。"""
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes))
        # 等比缩放，使最长边 = size，保留完整画面
        w, h = img.size
        ratio = size / max(w, h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), PILImage.LANCZOS)
        # 创建透明画布，居中粘贴
        canvas = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
        offset_x = (size - new_w) // 2
        offset_y = (size - new_h) // 2
        # 确保 img 有 alpha 通道再粘贴
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        canvas.paste(img, (offset_x, offset_y), img)
        buf = io.BytesIO()
        # GIF 只支持调色板单色透明：提取 alpha → 转 P 模式 → 指定透明索引
        alpha = canvas.split()[3]  # 提取 alpha 通道
        canvas = canvas.convert("RGB").convert("P", palette=PILImage.ADAPTIVE, colors=255)
        # 找到未使用的调色板索引作为透明色（用 255）
        mask = PILImage.eval(alpha, lambda a: 255 if a <= 128 else 0)
        canvas.paste(255, mask=mask)
        canvas.save(buf, format="GIF", transparency=255)
        return buf.getvalue()

    async def _send_image(
        self, event: AstrMessageEvent, image_bytes: bytes, sticker: bool | None = None,
    ) -> str | int | None:
        """发送图片到对应会话，返回 message_id（如果平台支持）。

        sticker: 是否以小图模式发送。None=使用心情表情的 sticker_mode 设置。
        """
        try:
            use_sticker = sticker if sticker is not None else self.settings.sticker_mode
            if use_sticker:
                image_bytes = await asyncio.to_thread(self._to_sticker, image_bytes)
            msg_id = None
            # aiocqhttp: 直接调用 bot API 以获取 message_id
            if hasattr(event, "bot") and hasattr(event.bot, "send_group_msg"):
                import base64 as b64mod
                b64_str = b64mod.b64encode(image_bytes).decode()
                seg = {"type": "image", "data": {"file": f"base64://{b64_str}"}}
                if use_sticker:
                    seg["data"]["subType"] = "7"
                group_id = event.get_group_id()
                if group_id:
                    ret = await event.bot.send_group_msg(
                        group_id=int(group_id), message=[seg]
                    )
                else:
                    ret = await event.bot.send_private_msg(
                        user_id=int(event.get_sender_id()), message=[seg]
                    )
                if isinstance(ret, dict):
                    msg_id = ret.get("message_id")
            elif event.get_platform_name() == "gewechat":
                await event.send(MessageChain([Image.fromBytes(image_bytes)]))
            else:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Image.fromBytes(image_bytes)]),
                )
            logger.info(f"[MemeMemPlus] 表情图片已发送, msg_id={msg_id}")
            return msg_id
        except Exception:
            logger.error(f"[MemeMemPlus] 发送图片失败: {traceback.format_exc()}")
            return None

    def _save_generated_image(self, mood: str, image_bytes: bytes) -> None:
        """将生成的图片保存到对应心情目录，并刷新缓存。"""
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
            f"表达门槛: {self.settings.expression_threshold}",
            f"大模型生图: {'开启' if self.settings.llm_generation_enabled else '关闭'}",
            f"生图概率: {self.settings.llm_generation_probability}%",
            f"图库: {len(stats)} 个心情, {non_empty} 个有参考图, 共 {total} 张",
            "",
            f"--- NovelAI 模式 ---",
            f"状态: {'开启（替代心情表情）' if self.settings.novelai_enabled else '关闭'}",
            f"参考图: {'已上传' if self.novelai_gen.has_reference else '未上传'}"
            f"{'（' + {'img2img': 'img2img', 'director': 'Precise Ref', 'vibe_transfer': 'Vibe Transfer'}.get(self.settings.novelai_reference_mode, self.settings.novelai_reference_mode) + '）' if self.settings.novelai_use_reference else '（未启用）'}",
            f"触发概率: {self.settings.novelai_probability}%",
            f"冷却: {self.settings.novelai_cooldown_seconds}秒",
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
            if count == 0:
                mode = "空（不发送）"
            else:
                mode = "抽取" + ("＋可生图" if self.settings.llm_generation_enabled else "")
            lines.append(f"  {mood}: {count}张 ({mode})")

        yield event.plain_result("\n".join(lines))

    @filter.command("心情表情刷新")
    async def refresh_library(self, event: AstrMessageEvent):
        """重新扫描图库目录，同步已删除图片的黑名单，清空 NAI 缓存。"""
        self.library_mgr.refresh()
        self.auto_updater.reload_seen_ids()
        self.novelai_gen.clear_caches()
        stats = self.library_mgr.get_stats()
        total = sum(stats.values())
        blocked = self.auto_updater.seen_ids_count
        yield event.plain_result(
            f"图库已刷新: {len(stats)} 个心情, 共 {total} 张参考图\n"
            f"已记录 {blocked} 个 booru ID（含回收站）\n"
            f"NAI 缓存已清空"
        )

    @filter.command("ni重置")
    async def reset_nai_caches(self, event: AstrMessageEvent):
        """清空 NovelAI 对话历史和穿搭缓存。"""
        self.novelai_gen.clear_caches()
        yield event.plain_result("NAI 对话历史和穿搭缓存已清空")

    @filter.command("穿搭")
    async def toggle_outfit(self, event: AstrMessageEvent, flag: str = ""):
        """运行时切换穿搭注入。/穿搭 0 关闭，/穿搭 {任意值} 开启。"""
        if flag.strip() == "0":
            self.settings.novelai_use_outfit = False
            yield event.plain_result("穿搭注入已关闭（缓存保留，重新开启后立即可用）")
        elif flag.strip():
            self.settings.novelai_use_outfit = True
            cached = self.novelai_gen._cached_outfit_tags
            if cached:
                yield event.plain_result(f"穿搭注入已开启\n当前缓存: {cached}")
            else:
                yield event.plain_result("穿搭注入已开启，下次生图时自动获取穿搭")
        else:
            status = "开启" if self.settings.novelai_use_outfit else "关闭"
            # 诊断：检查 life_scheduler 连接状态
            gen = self.novelai_gen
            raw_outfit = gen._get_raw_outfit()
            plugin_found = gen._life_plugin is not None
            # 主动刷新穿搭缓存（检测 life_scheduler 的更新）
            if self.settings.novelai_use_outfit and self.settings.novelai_llm_enabled and raw_outfit:
                from .utils.provider_helper import load_mood_provider
                cfg = load_mood_provider(self.context, self.settings, self.settings.novelai_llm_provider_id)
                if cfg.valid:
                    await gen._refresh_outfit_tags(cfg)
            outfit_tags = gen._cached_outfit_tags or "无"
            # 汇总所有会话的对话历史
            all_history = dict(gen._msg_history)  # snapshot
            total_msgs = sum(len(q) for q in all_history.values())
            num_sessions = len(all_history)
            history_str = f"{total_msgs} 条 ({num_sessions} 个会话)" if total_msgs else "空"
            # 上次适配输出的 tags
            last_adapted = dict(gen._last_adapted_tags)  # snapshot 防止并发修改
            adapted_count = len(last_adapted)
            enabled = self.settings.enabled
            nai_on = self.settings.novelai_enabled
            llm_on = self.settings.novelai_llm_enabled
            adapt_on = self.settings.novelai_outfit_adapt
            lines = [
                f"插件总开关: {'开启' if enabled else '关闭（自动生图被禁用！）'}",
                f"NovelAI 模式: {'开启' if nai_on else '关闭'}",
                f"LLM 标签补全: {'开启' if llm_on else '关闭（穿搭需要 LLM 补全开启）'}",
                f"穿搭注入: {status}",
                f"穿搭 tags: {outfit_tags}",
                f"穿搭情景适配: {'开启' if adapt_on else '关闭'} (参考最近 {self.settings.novelai_outfit_history} 条对话)",
                f"上次适配输出: {list(last_adapted.values())[0][:80] if adapted_count == 1 else f'{adapted_count} 个会话有记录' if adapted_count else '无'}",
                f"对话历史: {history_str}",
                f"--- 诊断 ---",
                f"life_scheduler: {'已找到' if plugin_found else '未找到'}",
                f"穿搭原文: {raw_outfit or '无'}",
                f"概率: {self.settings.novelai_probability}% | 冷却: {self.settings.novelai_cooldown_seconds}s",
            ]
            yield event.plain_result("\n".join(lines))

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

        async def _do_update():
            try:
                result = await self.auto_updater._run_once()
                msg = _format_search_result(result, prefix="自动搜图")
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(msg),
                )
            except Exception:
                logger.error(f"[MemeMemPlus] 自动搜图后台任务异常: {traceback.format_exc()}")

        self._launch_bg_task(_do_update())

    @filter.command("搜图")
    async def manual_search(self, event: AstrMessageEvent, count: int = 5):
        """手动搜图：搜图 N，使用配置的标签搜索 N 张图片并分类入库。"""
        count = min(max(1, count), 50)
        source = self.settings.auto_update_source.lower()
        if source == "pixiv":
            keyword = self.settings.pixiv_search_keyword.strip()
            search_word = keyword if keyword else self.settings.auto_update_search_tags
            search_target = self.settings.pixiv_search_target
            info = f"关键词: {search_word}\n来源: pixiv ({search_target})"
        else:
            info = f"标签: {self.settings.auto_update_search_tags}\n来源: {source}"
        yield event.plain_result(f"开始搜索 {count} 张图片...\n{info}")

        async def _do_search():
            try:
                result = await self.auto_updater._run_once(limit_override=count)
                msg = _format_search_result(result)
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(msg),
                )
            except Exception:
                logger.error(f"[MemeMemPlus] 手动搜图后台任务异常: {traceback.format_exc()}")

        self._launch_bg_task(_do_search())

    @filter.command("ni")
    async def novelai_direct(self, event: AstrMessageEvent, tags: GreedyStr):
        """直接用指定标签调用 NovelAI 生图，跳过 LLM。

        用法: /ni <正向标签>       → 发送原图
              /ni 0 <正向标签>     → 发送小图(GIF贴纸)
        正向标签由用户提供，负向标签使用配置值。
        """
        if not tags.strip():
            yield event.plain_result("用法: /ni <正向标签>\n       /ni 0 <正向标签>  (小图模式)")
            return
        if not self.settings.novelai_api_key:
            yield event.plain_result("未配置 NovelAI API Key")
            return

        # 解析小图模式: /ni 0 xxx → sticker=True, 否则 sticker=False
        stripped = tags.strip()
        if stripped == "0" or stripped.startswith("0 ") or stripped.startswith("0,"):
            use_sticker = True
            tags = stripped[1:].strip().lstrip(",").strip()
            if not tags:
                yield event.plain_result("用法: /ni 0 <正向标签>\n例如: /ni 0 1girl, smile")
                return
        else:
            use_sticker = False

        # 原图模式使用独立模型（如果配置了），小图模式使用默认模型
        model_override = None
        if not use_sticker and self.settings.novelai_direct_model:
            model_override = self.settings.novelai_direct_model

        logger.info(f"[MemeMemPlus-NAI] /ni 收到标签: '{tags}', 小图模式={use_sticker}, 模型={model_override or '默认'}")
        yield event.plain_result(f"NovelAI 直接生图中...{'(小图模式)' if use_sticker else ''}")

        async def _do_ni():
            try:
                image_bytes, save_path = await self.novelai_gen.run_direct(tags, model_override=model_override)
                if not image_bytes:
                    try:
                        await self.context.send_message(
                            event.unified_msg_origin,
                            MessageChain().message("NovelAI 生图失败"),
                        )
                    except Exception:
                        pass
                    return
                sent_msg_id = await self._send_image(event, image_bytes, sticker=use_sticker)
                if save_path:
                    self._record_last_sent(event.unified_msg_origin, Path(save_path), sent_msg_id)
                logger.info(f"[MemeMemPlus-NAI] /ni 生图完成, saved={save_path}")
            except Exception:
                logger.error(f"[MemeMemPlus-NAI] /ni 后台任务异常: {traceback.format_exc()}")

        self._launch_bg_task(_do_ni())

    @filter.command("删图")
    async def delete_meme(self, event: AstrMessageEvent):
        """删除不想要的表情图片，移入 trash 目录。

        用法：
        - 发送 /删图 并附带图片 → 按哈希匹配图库中的图片
        - 回复一张 bot 发的表情图并发送 /删图 → 匹配回复中的图片
        - 直接发送 /删图 → 删除本会话最近一次发送的表情图
        """
        target_path = None
        recall_msg_id = None

        # 1. 尝试从消息中直接获取图片
        img_comp = self._find_image_in_chain(event.message_obj.message)
        if img_comp:
            target_path = await self._match_image_from_component(img_comp)

        # 2. 尝试从回复的消息中获取图片
        if not target_path:
            from astrbot.core.message.components import Reply as ReplyComp
            for comp in event.message_obj.message:
                if isinstance(comp, ReplyComp) and comp.chain:
                    img_comp = self._find_image_in_chain(comp.chain)
                    if img_comp:
                        target_path = await self._match_image_from_component(img_comp)
                        if target_path:
                            recall_msg_id = comp.id  # 回复的消息ID，用于撤回
                    break

        # 3. 回退到最近发送的图片
        if not target_path:
            last = self._last_sent.get(event.unified_msg_origin)
            if last:
                target_path, recall_msg_id = last

        if not target_path or not target_path.exists():
            yield event.plain_result("未找到可删除的图片。请附带图片、回复表情图、或在发送表情后使用此命令。")
            return

        # 移入 trash
        dest = self.trash_dir / target_path.name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = self.trash_dir / f"{stem}_{hashlib.md5(target_path.read_bytes()).hexdigest()[:6]}{suffix}"

        shutil.move(str(target_path), str(dest))
        self.library_mgr.refresh()

        # 尝试撤回消息
        recalled = await self._try_recall(event, recall_msg_id)

        # 清除 last_sent 记录
        last = self._last_sent.get(event.unified_msg_origin)
        if last and last[0] == target_path:
            del self._last_sent[event.unified_msg_origin]

        mood = target_path.parent.name
        logger.info(f"[MemeMemPlus] 已删图: {target_path.name} (mood={mood}) → trash/, 撤回={'成功' if recalled else '跳过'}")
        recall_text = "，已撤回消息" if recalled else ""
        yield event.plain_result(f"已删除: {target_path.name}\n来源心情: {mood}\n已移入回收站{recall_text}。")

    # ── Tag 预设管理 ─────────────────────────────────────────

    _TAG_SETTING_KEYS = (
        "novelai_base_tags", "novelai_negative_prompt", "novelai_custom_tags",
        "novelai_r18_custom_tags", "novelai_r18_nude_tags", "novelai_r18_nude_negative",
    )

    def _get_outfit_snapshot(self) -> tuple[str, str]:
        """返回 (穿搭原文, 穿搭tags)，优先用缓存，缓存为空则主动拉取原文。"""
        text = self.novelai_gen._cached_outfit_text
        tags = self.novelai_gen._cached_outfit_tags
        if not text:
            text = self.novelai_gen._get_raw_outfit() or ""
        return text, tags

    def _write_outfit_to_life_scheduler(self, outfit_text: str) -> bool:
        """把穿搭原文写回 life_scheduler 的 schedule 对象，返回是否成功。"""
        if not outfit_text:
            return False
        gen = self.novelai_gen
        if not gen._life_plugin:
            gen._get_raw_outfit()  # 触发插件发现
        if not gen._life_plugin:
            return False
        try:
            import datetime as _dt
            data_mgr = getattr(gen._life_plugin, "data_mgr", None)
            if not data_mgr:
                return False
            # 跨日回退：0 点后今日数据未生成时写回昨日数据
            now = _dt.datetime.now()
            schedule = None
            for offset in range(4):
                s = data_mgr.get(now - _dt.timedelta(days=offset))
                if s and getattr(s, "status", "") != "failed":
                    schedule = s
                    break
            if not schedule:
                return False
            schedule.outfit = outfit_text
            schedule.outfit_style = ""
            data_mgr.set(schedule)
            logger.info(f"[MemeMemPlus-NAI] 穿搭已写回 life_scheduler: {outfit_text[:40]}")
            return True
        except Exception as e:
            logger.debug(f"[MemeMemPlus-NAI] 写回穿搭到 life_scheduler 失败: {e}")
            return False

    def _save_tag_preset(self, name: str) -> Path:
        """保存当前 tag 设置 + 穿搭缓存为 JSON 预设。"""
        outfit_text, outfit_tags = self._get_outfit_snapshot()
        data = {
            "name": name,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "settings": {k: getattr(self.settings, k) for k in self._TAG_SETTING_KEYS},
            "outfit_cache": {
                "cached_outfit_text": outfit_text,
                "cached_outfit_tags": outfit_tags,
            },
        }
        path = self._tag_preset_dir / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _load_tag_preset(self, name: str) -> dict | None:
        """按名称加载预设 JSON，不存在或解析失败返回 None。"""
        path = self._tag_preset_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "settings" not in data:
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    @filter.command("存tag")
    async def save_tag_preset(self, event: AstrMessageEvent, name: str = ""):
        """保存当前 NovelAI 正负标签 + 穿搭缓存为命名预设。"""
        name = name.strip()
        if not name:
            yield event.plain_result("用法: /存tag <预设名称>")
            return
        if "/" in name or "\\" in name or ".." in name:
            yield event.plain_result("预设名称不能包含路径字符")
            return
        existed = (self._tag_preset_dir / f"{name}.json").exists()
        self._save_tag_preset(name)
        action = "覆盖" if existed else "保存"
        outfit_text, outfit_tags = self._get_outfit_snapshot()
        lines = [
            f"Tag 预设已{action}: {name}",
            f"正向: {self.settings.novelai_base_tags[:60]}...",
            f"穿搭原文: {outfit_text[:60] + '...' if outfit_text else '无'}",
            f"穿搭标签: {outfit_tags[:60] + '...' if outfit_tags else '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("应用tag")
    async def apply_tag_preset(self, event: AstrMessageEvent, name: str = ""):
        """加载已保存的 tag 预设，替换当前设置和穿搭缓存。"""
        name = name.strip()
        if not name:
            yield event.plain_result("用法: /应用tag <预设名称>")
            return
        data = self._load_tag_preset(name)
        if not data:
            yield event.plain_result(f"未找到预设: {name}")
            return
        saved = data["settings"]
        for k in self._TAG_SETTING_KEYS:
            if k in saved:
                setattr(self.settings, k, saved[k])
        oc = data.get("outfit_cache", {})
        cached_text = oc.get("cached_outfit_text", "")
        self.novelai_gen._cached_outfit_text = cached_text
        self.novelai_gen._cached_outfit_tags = oc.get("cached_outfit_tags", "")
        self.novelai_gen._last_adapted_tags.clear()
        self.novelai_gen._msg_history.clear()
        # 写回 life_scheduler，使 _get_raw_outfit() 返回值与缓存一致
        life_ok = self._write_outfit_to_life_scheduler(cached_text)
        lines = [
            f"已应用预设: {name}",
            f"正向: {self.settings.novelai_base_tags[:60]}...",
            f"穿搭原文: {cached_text[:60] + '...' if cached_text else '无'}",
            f"穿搭标签: {self.novelai_gen._cached_outfit_tags[:60] + '...' if self.novelai_gen._cached_outfit_tags else '无'}",
        ]
        if life_ok:
            lines.append("穿搭已同步到 life_scheduler")
        elif cached_text:
            lines.append("穿搭同步到 life_scheduler 失败（插件未找到）")
        yield event.plain_result("\n".join(lines))

    @filter.command("查看所有tag")
    async def list_tag_presets(self, event: AstrMessageEvent):
        """列出所有已保存的 tag 预设。"""
        files = sorted(self._tag_preset_dir.glob("*.json"))
        if not files:
            yield event.plain_result("暂无保存的 tag 预设")
            return
        lines = [f"=== Tag 预设列表 ({len(files)} 个) ==="]
        for f in files:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                base = d.get("settings", {}).get("novelai_base_tags", "")[:40]
                has_outfit = "有" if d.get("outfit_cache", {}).get("cached_outfit_tags") else "无"
                lines.append(f"  {f.stem} — {base}... (穿搭:{has_outfit})")
            except Exception:
                lines.append(f"  {f.stem} — (读取失败)")
        yield event.plain_result("\n".join(lines))

    @filter.command("查看tag")
    async def view_tag_preset(self, event: AstrMessageEvent, name: str = ""):
        """查看某个 tag 预设的详细内容。"""
        name = name.strip()
        if not name:
            yield event.plain_result("用法: /查看tag <预设名称>")
            return
        data = self._load_tag_preset(name)
        if not data:
            yield event.plain_result(f"未找到预设: {name}")
            return
        s = data["settings"]
        oc = data.get("outfit_cache", {})
        lines = [
            f"=== 预设: {data.get('name', name)} ===",
            f"创建时间: {data.get('created_at', '未知')}",
            f"正向标签: {s.get('novelai_base_tags', '')}",
            f"负向标签: {s.get('novelai_negative_prompt', '')[:80]}...",
            f"自定义标签: {s.get('novelai_custom_tags') or '无'}",
            f"R18 自定义: {s.get('novelai_r18_custom_tags') or '无'}",
            f"R18 裸体正向: {s.get('novelai_r18_nude_tags', '')[:60]}...",
            f"R18 裸体负向: {s.get('novelai_r18_nude_negative', '')[:60]}...",
            f"穿搭原文: {oc.get('cached_outfit_text') or '无'}",
            f"穿搭标签: {oc.get('cached_outfit_tags') or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("删除tag")
    async def delete_tag_preset(self, event: AstrMessageEvent, name: str = ""):
        """删除一个已保存的 tag 预设。"""
        name = name.strip()
        if not name:
            yield event.plain_result("用法: /删除tag <预设名称>")
            return
        path = self._tag_preset_dir / f"{name}.json"
        if not path.exists():
            yield event.plain_result(f"未找到预设: {name}")
            return
        path.unlink()
        yield event.plain_result(f"已删除预设: {name}")

    async def _try_recall(self, event: AstrMessageEvent, msg_id: str | int | None) -> bool:
        """尝试撤回消息，成功返回 True。"""
        if not msg_id:
            return False
        try:
            # aiocqhttp (OneBot v11)
            if hasattr(event, "bot") and hasattr(event.bot, "call_action"):
                await event.bot.call_action("delete_msg", message_id=int(msg_id))
                return True
        except Exception:
            logger.warning(f"[MemeMemPlus] 撤回消息失败 (msg_id={msg_id}): {traceback.format_exc()}")
        return False

    def _find_image_in_chain(self, chain: list | None) -> "Image | None":
        """从消息链中找到第一个 Image 组件。"""
        if not chain:
            return None
        for comp in chain:
            if isinstance(comp, Image):
                return comp
        return None

    async def _match_image_from_component(self, img: Image) -> Path | None:
        """下载图片并在图库中按哈希匹配。"""
        try:
            file_path = await img.convert_to_file_path()
            image_bytes = Path(file_path).read_bytes()
            return self.library_mgr.find_by_hash(image_bytes)
        except Exception:
            logger.warning(f"[MemeMemPlus] 图片匹配失败: {traceback.format_exc()}")
            return None

