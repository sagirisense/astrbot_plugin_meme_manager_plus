import asyncio
import collections
import hashlib
import io
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

        # 删图功能：trash 目录（放在插件根目录，不在 memes/ 下）
        self.trash_dir = self.plugin_dir / "trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        # NovelAI 生图模式（独立）
        self.novelai_gen = NovelAIGenerator(self.settings, context, self.plugin_dir)
        self.novelai_cooldown = CooldownManager(
            self.settings.novelai_cooldown_seconds, self.settings.per_group
        )

        # 记录每个会话最近发送的图片路径和消息ID，用于 /删图
        # 格式: {unified_msg_origin: (file_path, message_id_or_None)}
        # OrderedDict 保证 FIFO 淘汰顺序
        self._last_sent: collections.OrderedDict[str, tuple[Path, str | int | None]] = collections.OrderedDict()
        self._MAX_LAST_SENT = 200  # 防止内存泄漏

        # 保存后台任务引用，防止被 GC 回收导致异常丢失
        self._bg_tasks: set[asyncio.Task] = set()

        logger.info(
            f"[MemeMemPlus] 插件初始化完成, "
            f"启用={self.settings.enabled}, "
            f"自动更新={'开' if self.settings.auto_update_enabled else '关'}, "
            f"图库心情数={len(self.library_mgr.get_all_moods())}"
        )

    def _launch_bg_task(self, coro) -> asyncio.Task:
        """创建后台任务并持有引用，任务完成后自动移除。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _on_unload(self):
        """插件卸载时清理资源。"""
        self.auto_updater.stop()
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
        group_id = str(getattr(event.message_obj, "group_id", "")) or None

        # NovelAI 模式：开启时走独立流程，跳过心情表情
        if self.settings.novelai_enabled:
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
                image_bytes = picked.read_bytes()
                logger.info(f"[MemeMemPlus] 随机抽取: {picked.name}, mood={mood}")
                sent_msg_id = await self._send_image(event, image_bytes)
                self._last_sent[event.unified_msg_origin] = (picked, sent_msg_id)
                # 防止 _last_sent 无限增长
                while len(self._last_sent) > self._MAX_LAST_SENT:
                    self._last_sent.popitem(last=False)

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
            image_bytes, save_path = await self.novelai_gen.run(text)
            if not image_bytes:
                logger.warning("[MemeMemPlus-NAI] 生图失败")
                return

            await self._send_image(event, image_bytes, sticker=self.settings.novelai_sticker_mode)
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
                image_bytes = self._to_sticker(image_bytes)
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
        """重新扫描图库目录，同步已删除图片的黑名单。"""
        self.library_mgr.refresh()
        self.auto_updater.reload_seen_ids()
        stats = self.library_mgr.get_stats()
        total = sum(stats.values())
        blocked = self.auto_updater.seen_ids_count
        yield event.plain_result(
            f"图库已刷新: {len(stats)} 个心情, 共 {total} 张参考图\n"
            f"已记录 {blocked} 个 booru ID（含回收站）"
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

        async def _do_update():
            result = await self.auto_updater._run_once()
            try:
                msg = _format_search_result(result, prefix="自动搜图")
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(msg),
                )
            except Exception:
                logger.error(f"[MemeMemPlus] 发送搜图结果失败: {traceback.format_exc()}")

        self._launch_bg_task(_do_update())

    @filter.command("搜图")
    async def manual_search(self, event: AstrMessageEvent, count: int = 5):
        """手动搜图：搜图 N，使用配置的标签搜索 N 张图片并分类入库。"""
        count = min(max(1, count), 50)
        source = self.settings.auto_update_source.lower()
        if source == "pixiv":
            keyword = getattr(self.settings, "pixiv_search_keyword", "").strip()
            search_word = keyword if keyword else self.settings.auto_update_search_tags
            search_target = getattr(self.settings, "pixiv_search_target", "partial_match_for_tags")
            info = f"关键词: {search_word}\n来源: pixiv ({search_target})"
        else:
            info = f"标签: {self.settings.auto_update_search_tags}\n来源: {source}"
        yield event.plain_result(f"开始搜索 {count} 张图片...\n{info}")

        async def _do_search():
            result = await self.auto_updater._run_once(limit_override=count)
            try:
                msg = _format_search_result(result)
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(msg),
                )
            except Exception:
                logger.error(f"[MemeMemPlus] 发送搜图结果失败: {traceback.format_exc()}")

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
            await self._send_image(event, image_bytes, sticker=use_sticker)
            logger.info(f"[MemeMemPlus-NAI] /ni 生图完成, saved={save_path}")

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

    def _find_image_in_chain(self, chain: list) -> "Image | None":
        """从消息链中找到第一个 Image 组件。"""
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

