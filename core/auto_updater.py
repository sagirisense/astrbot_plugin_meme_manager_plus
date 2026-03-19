"""自动图库更新器：定时从 Booru 图站搜索图片，LLM 分类心情后入库。"""

import asyncio
import base64
import hashlib
import io
import random
import traceback
from pathlib import Path

import aiohttp
from PIL import Image

from astrbot.api import logger

from ..config.settings import PluginSettings
from ..utils.provider_helper import load_mood_provider
from ..utils.llm_client import LLMClient

# Booru API 端点
BOORU_APIS = {
    "yandere": "https://yande.re/post.json",
    "konachan": "https://konachan.com/post.json",
    "danbooru": "https://danbooru.donmai.us/posts.json",
}

# 作品页 URL 模板（用于日志输出可直接点开的链接）
POST_PAGE_URLS = {
    "yandere": "https://yande.re/post/show/{id}",
    "konachan": "https://konachan.com/post/show/{id}",
    "danbooru": "https://danbooru.donmai.us/posts/{id}",
    "pixiv": "https://www.pixiv.net/artworks/{id}",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class AutoUpdater:
    """定时从 Booru 搜索角色图片，用 LLM 判断心情后保存到对应目录。"""

    # 类级别：确保全局只有一个定时循环在运行（防止插件热重载时多个循环叠加）
    _global_task: asyncio.Task | None = None
    _global_stop_event: asyncio.Event | None = None

    def __init__(self, settings: PluginSettings, context, library_mgr):
        self.settings = settings
        self.context = context
        self.library_mgr = library_mgr
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()  # 防止并发搜图
        self._seen_lock = asyncio.Lock()  # 保护 _seen_ids 并发修改
        self._MAX_SEEN_IDS = 10000  # 防止内存泄漏
        # 从磁盘已有文件恢复已下载 ID，避免重复搜索
        self._seen_ids: set[str] = self._load_seen_ids()

    def _load_seen_ids(self) -> set[str]:
        """扫描图库和 trash 中 booru_ 开头的文件，提取 post ID。"""
        ids = set()
        scan_dirs = [self.library_mgr.library_dir]
        # 也扫描 trash 目录，防止已删除的图被重新下载
        trash_dir = self.library_mgr.library_dir.parent / "trash"
        if trash_dir.exists():
            scan_dirs.append(trash_dir)
        for base_dir in scan_dirs:
            if not base_dir.exists():
                continue
            # trash 是平级目录，直接扫描其中的文件
            for f in base_dir.iterdir():
                if f.is_file() and f.name.startswith("booru_"):
                    parts = f.stem.split("_")
                    if len(parts) >= 2:
                        ids.add(parts[1])
                # memes/ 下是子目录
                if f.is_dir():
                    for sub_f in f.iterdir():
                        if sub_f.is_file() and sub_f.name.startswith("booru_"):
                            parts = sub_f.stem.split("_")
                            if len(parts) >= 2:
                                ids.add(parts[1])
        if ids:
            logger.info(f"[MemeMemPlus] 从磁盘恢复 {len(ids)} 个已下载图片 ID")
        return ids

    def start(self) -> None:
        """启动后台定时任务。先停止全局已有的旧循环，防止热重载叠加。"""
        # 停止全局旧循环（如果存在）——防止插件重载时多个 _loop 同时运行
        if AutoUpdater._global_task and not AutoUpdater._global_task.done():
            logger.info("[MemeMemPlus] 检测到旧的自动更新循环，先停止")
            if AutoUpdater._global_stop_event:
                AutoUpdater._global_stop_event.set()
            AutoUpdater._global_task.cancel()
            AutoUpdater._global_task = None

        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        # 记录到类变量，供下次热重载时清理
        AutoUpdater._global_task = self._task
        AutoUpdater._global_stop_event = self._stop_event
        logger.info(
            f"[MemeMemPlus] 自动更新已启动: "
            f"间隔={self.settings.auto_update_interval_hours}h, "
            f"标签={self.settings.auto_update_search_tags}, "
            f"来源={self.settings.auto_update_source}"
        )

    def stop(self) -> None:
        """停止后台任务。"""
        self._stop_event.set()
        task = self._task
        if task and not task.done():
            task.cancel()
        self._task = None
        # 同步清理类变量
        if AutoUpdater._global_task is task and (
            AutoUpdater._global_stop_event is self._stop_event
        ):
            AutoUpdater._global_task = None
            AutoUpdater._global_stop_event = None
        logger.info("[MemeMemPlus] 自动更新已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def seen_ids_count(self) -> int:
        """已记录的图片 ID 数量（供状态显示用）。"""
        return len(self._seen_ids)

    def reload_seen_ids(self) -> None:
        """从磁盘重建已下载 ID 集合。"""
        self._seen_ids = self._load_seen_ids()

    async def _loop(self) -> None:
        """主循环：等待间隔 → 执行一轮搜图。"""
        # 首次启动延迟 30 秒，避免和初始化抢资源
        await self._wait(30)

        while not self._stop_event.is_set():
            # 每轮重新读取间隔，以便用户改配置后立即生效
            interval = max(0.5, self.settings.auto_update_interval_hours) * 3600
            try:
                await self._run_once()
            except Exception:
                logger.error(f"[MemeMemPlus] 自动更新异常: {traceback.format_exc()}")
            logger.info(f"[MemeMemPlus] 自动更新: 下次执行在 {interval/3600:.1f} 小时后")
            await self._wait(interval)

    async def _wait(self, seconds: float) -> None:
        """可中断的等待。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_once(self, limit_override: int | None = None) -> dict:
        """执行一轮搜索。返回统计 dict: {saved, searched, dl_fail, filtered, mood_fail, total, skipped}。"""
        if self._lock.locked():
            logger.info("[MemeMemPlus] 自动更新: 已有搜图任务在执行，跳过")
            return {"skipped": "busy"}
        async with self._lock:
            return await self._run_once_inner(limit_override)

    async def _run_once_inner(self, limit_override: int | None = None) -> dict:
        # 图库总上限检查
        max_lib = self.settings.max_library_size
        if max_lib > 0:
            total = sum(self.library_mgr.get_stats().values())
            if total >= max_lib:
                logger.info(f"[MemeMemPlus] 图库已达上限({total}>={max_lib})，跳过搜图")
                return {"skipped": "library_full", "total": total}

        logger.info("[MemeMemPlus] 自动更新: 开始搜索图片...")

        posts = await self._search_images(limit_override=limit_override)
        if not posts:
            logger.info("[MemeMemPlus] 自动更新: 未找到新图片")
            return {"saved": 0, "searched": 0}

        available_moods = self.library_mgr.get_all_moods()
        if not available_moods:
            logger.warning("[MemeMemPlus] 自动更新: 无可用心情标签")
            return {"skipped": "no_moods"}

        # ── 阶段1: 并发下载所有图片（复用 Session，信号量控制并发） ──
        dl_semaphore = asyncio.Semaphore(8)
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {}

        async def _dl_one(post: dict, session: aiohttp.ClientSession) -> tuple[dict, bytes | None]:
            async with dl_semaphore:
                if self._stop_event.is_set():
                    return post, None
                return post, await self._download_image_with_session(session, post["url"])

        downloaded: list[tuple[dict, bytes | None]] = []
        # 判断是否有 Pixiv 图片，统一设置 Referer
        has_pixiv = any("pximg.net" in p["url"] or "pixiv" in p["url"] for p in posts)
        if has_pixiv:
            headers["Referer"] = "https://www.pixiv.net/"

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            tasks = [_dl_one(post, session) for post in posts]
            downloaded = await asyncio.gather(*tasks)

        # 分离下载成功和失败
        dl_ok = [(post, img) for post, img in downloaded if img is not None]
        skipped_dl = len(downloaded) - len(dl_ok)
        logger.info(f"[MemeMemPlus] 并发下载完成: 成功 {len(dl_ok)}, 失败 {skipped_dl}")

        # ── 阶段2: 并发 LLM 筛选 + 分类（信号量控制并发避免限流） ──
        llm_semaphore = asyncio.Semaphore(4)
        source = self.settings.auto_update_source.lower()
        page_tpl = POST_PAGE_URLS.get(source, "")

        def _post_url(post_id: str) -> str:
            return page_tpl.format(id=post_id) if page_tpl else post_id

        async def _process_one(post: dict, image_bytes: bytes) -> tuple[str, dict, bytes] | None:
            """返回 (mood, post, image_bytes) 或 None。"""
            async with llm_semaphore:
                if self._stop_event.is_set():
                    return None
                # 筛选
                if self.settings.auto_update_filter_prompt:
                    if not await self._filter_image(image_bytes):
                        async with self._seen_lock:
                            self._seen_ids.add(post["id"])
                        logger.info(f"[MemeMemPlus] 自动更新: 图片被筛选拒绝 → {_post_url(post['id'])}")
                        return ("__filtered__", post, image_bytes)
                # 分类
                url = _post_url(post['id'])
                mood = await self._classify_mood(image_bytes, available_moods, post_url=url)
                if mood == "__api_error__":
                    # API 失败，不加入 seen_ids，下次可重试
                    return ("__no_mood__", post, image_bytes)
                if not mood:
                    async with self._seen_lock:
                        self._seen_ids.add(post["id"])
                    return ("__no_mood__", post, image_bytes)
                return (mood, post, image_bytes)

        process_tasks = [_process_one(post, img) for post, img in dl_ok]
        results = await asyncio.gather(*process_tasks, return_exceptions=True)

        saved = 0
        skipped_mood = 0
        skipped_filter = 0
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"[MemeMemPlus] 自动更新: 处理图片失败: {result}")
                continue
            if result is None:
                continue
            mood, post, image_bytes = result
            if mood == "__filtered__":
                skipped_filter += 1
            elif mood == "__no_mood__":
                skipped_mood += 1
            else:
                await self._save_image(mood, image_bytes, post["id"])
                saved += 1
                logger.info(f"[MemeMemPlus] 自动更新: 图片 → {mood} ← {_post_url(post['id'])}")

        self.library_mgr.refresh()
        total_in_library = sum(self.library_mgr.get_stats().values())
        logger.info(
            f"[MemeMemPlus] 自动更新完成: 搜索 {len(posts)} 张, "
            f"入库 {saved} 张, 下载失败 {skipped_dl}, 筛选拒绝 {skipped_filter}, "
            f"分类失败 {skipped_mood} | 图库总计 {total_in_library} 张"
        )
        return {
            "saved": saved,
            "searched": len(posts),
            "dl_fail": skipped_dl,
            "filtered": skipped_filter,
            "mood_fail": skipped_mood,
            "total": total_in_library,
        }

    # ── 搜索 ──────────────────────────────────────────────────

    async def _search_images(self, limit_override: int | None = None) -> list[dict]:
        """从 Booru/Pixiv 搜索图片，返回 [{id, url}, ...]。"""
        source = self.settings.auto_update_source.lower()
        limit = min(max(1, limit_override or self.settings.auto_update_images_per_cycle), 50)
        min_score = self.settings.auto_update_min_score

        if source == "pixiv":
            # Pixiv: 优先用专用关键词，没有则回退到通用搜索标签
            keyword = self.settings.pixiv_search_keyword.strip()
            search_word = keyword if keyword else self.settings.auto_update_search_tags.strip()
            search_target = self.settings.pixiv_search_target
            logger.info(
                f"[MemeMemPlus] 搜索参数: source=pixiv, word='{search_word}', "
                f"target={search_target}, limit={limit}, min_score={min_score}"
            )
            raw = await self._fetch_pixiv(search_word, search_target, limit)
        else:
            # Booru: 直接用搜索标签
            tags = self.settings.auto_update_search_tags.strip()
            logger.info(
                f"[MemeMemPlus] 搜索参数: source={source}, tags='{tags}', "
                f"limit={limit}, min_score={min_score}"
            )
            raw = await self._fetch_booru(source, tags, limit)

        # 统一过滤
        return self._filter_results(raw, limit, min_score, source)

    def _filter_results(
        self, raw: list[dict], limit: int, min_score: int, source: str
    ) -> list[dict]:
        """统一过滤：去重、评分、R18、格式。返回最多 limit 条。"""
        allow_r18 = self.settings.pixiv_allow_r18
        results = []
        skipped_seen = 0
        skipped_score = 0
        skipped_r18 = 0

        for item in raw:
            pid = item["id"]
            if pid in self._seen_ids:
                skipped_seen += 1
                continue
            if (item.get("score") or 0) < min_score:
                skipped_score += 1
                continue
            # Pixiv R18 过滤
            if source == "pixiv" and not allow_r18 and item.get("r18", False):
                skipped_r18 += 1
                continue
            url = item.get("url", "")
            if not url:
                continue
            ext = Path(url.split("?")[0]).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue

            results.append({"id": pid, "url": url})
            if len(results) >= limit:
                break

        random.shuffle(results)
        logger.info(
            f"[MemeMemPlus] 搜索筛选: 已见 {skipped_seen}, "
            f"低分 {skipped_score}, R18 {skipped_r18}, 可用 {len(results)}"
        )
        return results

    # ── Booru 搜索 ───────────────────────────────────────────

    async def _fetch_booru(self, source: str, tags: str, limit: int) -> list[dict]:
        """从 Booru 获取原始数据，返回统一格式 [{id, url, score}, ...]。"""
        api_url = BOORU_APIS.get(source)
        if not api_url:
            logger.error(f"[MemeMemPlus] 未知图片来源: {source}")
            return []

        raw = []
        max_pages = 20
        per_page = limit * 3

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for page in range(1, max_pages + 1):
                    params = {"tags": tags, "limit": per_page, "page": page}
                    async with session.get(api_url, params=params) as resp:
                        if resp.status != 200:
                            logger.error(
                                f"[MemeMemPlus] Booru API 错误 {resp.status}: "
                                f"{(await resp.text())[:200]}"
                            )
                            break
                        data = await resp.json()

                    if not data:
                        break
                    if page == 1:
                        logger.info(f"[MemeMemPlus] Booru 第1页返回 {len(data)} 条")

                    for post in data:
                        post_id = str(post.get("id", ""))
                        if source == "danbooru":
                            url = post.get("file_url") or post.get("large_file_url", "")
                        else:
                            url = post.get("jpeg_url") or post.get("file_url", "")
                        raw.append({
                            "id": post_id,
                            "url": url,
                            "score": post.get("score") or 0,
                        })

                    # 够了就不翻页 — 预估可用数需排除已见和低分
                    usable = sum(
                        1 for r in raw
                        if r["id"] not in self._seen_ids
                        and r.get("score", 0) >= self.settings.auto_update_min_score
                    )
                    if usable >= limit or len(data) < per_page:
                        break
        except Exception as e:
            logger.error(f"[MemeMemPlus] Booru 搜索失败: {e}")

        return raw

    # ── Pixiv 搜索 ───────────────────────────────────────────

    async def _fetch_pixiv(self, search_word: str, search_target: str, limit: int) -> list[dict]:
        """从 Pixiv 获取原始数据，返回统一格式 [{id, url, score, r18}, ...]。"""
        token = self.settings.pixiv_refresh_token.strip()
        if not token:
            logger.error("[MemeMemPlus] Pixiv 图源需要配置 pixiv_refresh_token")
            return []

        try:
            from pixivpy3 import AppPixivAPI
        except ImportError:
            logger.error("[MemeMemPlus] 缺少 pixivpy3，请运行: pip install pixivpy3")
            return []

        api = AppPixivAPI()
        api.set_accept_language("zh-cn")
        try:
            # pixivpy3 是同步库，放到线程池避免阻塞事件循环
            await asyncio.to_thread(api.auth, refresh_token=token)
        except Exception as e:
            logger.error(f"[MemeMemPlus] Pixiv 登录失败: {e}")
            return []

        logger.info(f"[MemeMemPlus] Pixiv 搜索: word='{search_word}', target={search_target}")

        raw = []
        next_url = None
        max_pages = 20

        for pg in range(max_pages):
            try:
                if pg == 0:
                    resp = await asyncio.to_thread(
                        api.search_illust,
                        search_word,
                        search_target=search_target,
                        sort="popular_desc",
                    )
                else:
                    if not next_url:
                        break
                    qs = api.parse_qs(next_url)
                    if not qs:
                        break
                    resp = await asyncio.to_thread(api.search_illust, **qs)
            except Exception as e:
                logger.error(f"[MemeMemPlus] Pixiv 搜索失败 (page {pg}): {e}")
                break

            illusts = resp.get("illusts", [])
            if not illusts:
                break
            if pg == 0:
                logger.info(f"[MemeMemPlus] Pixiv 返回 {len(illusts)} 条结果")
            next_url = resp.get("next_url")

            for illust in illusts:
                pid = str(illust["id"])
                page_count = illust.get("page_count", 1)
                if page_count == 1:
                    url = (
                        illust.get("meta_single_page", {}).get("original_image_url")
                        or illust.get("image_urls", {}).get("large", "")
                    )
                else:
                    pages = illust.get("meta_pages", [])
                    url = pages[0].get("image_urls", {}).get("original", "") if pages else ""

                raw.append({
                    "id": pid,
                    "url": url,
                    "score": illust.get("total_bookmarks") or 0,
                    "r18": illust.get("x_restrict", 0) > 0,
                })

            # 够了就不翻页 — 预估可用数需排除已见、低分、R18
            allow_r18 = self.settings.pixiv_allow_r18
            usable = sum(
                1 for r in raw
                if r["id"] not in self._seen_ids
                and r.get("score", 0) >= self.settings.auto_update_min_score
                and (allow_r18 or not r.get("r18", False))
            )
            if usable >= limit:
                break

        return raw

    # ── 下载 ──────────────────────────────────────────────────

    async def _download_image_with_session(
        self, session: aiohttp.ClientSession, url: str
    ) -> bytes | None:
        """使用已有 Session 下载图片，大图自动压缩。硬上限 50MB 防止内存爆炸。"""
        MAX_RAW = 50 * 1024 * 1024       # 原始下载上限 50MB
        COMPRESS_THRESHOLD = 5 * 1024 * 1024  # 超过 5MB 自动压缩
        TARGET_MAX_DIM = 1600             # 压缩后最长边
        JPEG_QUALITY = 85

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.info(f"[MemeMemPlus] 下载失败: HTTP {resp.status} ← {url[:120]}")
                    return None
                size = int(resp.headers.get("Content-Length", 0))
                if size > MAX_RAW:
                    logger.info(f"[MemeMemPlus] 图片超过50MB跳过: {size // 1024 // 1024}MB ← {url[:120]}")
                    return None
                raw = await resp.read()

            # 小图直接返回
            if len(raw) <= COMPRESS_THRESHOLD:
                return raw

            # 大图压缩（CPU 密集，放到线程池避免阻塞事件循环）
            compressed = await asyncio.to_thread(self._compress_image, raw)
            if compressed:
                logger.info(
                    f"[MemeMemPlus] 大图已压缩: {len(raw) // 1024}KB → {len(compressed) // 1024}KB ← {url[:80]}"
                )
            return compressed

        except asyncio.TimeoutError:
            logger.info(f"[MemeMemPlus] 下载超时(60s) ← {url[:120]}")
            return None
        except Exception as e:
            logger.info(f"[MemeMemPlus] 下载异常: {type(e).__name__}: {e} ← {url[:120]}")
            return None

    @staticmethod
    def _compress_image(raw: bytes) -> bytes | None:
        """CPU 密集的 PIL 压缩，应在 to_thread 中调用。"""
        try:
            img = Image.open(io.BytesIO(raw))
            img.thumbnail((1600, 1600), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception:
            return None

    # ── LLM 筛选与分类 ───────────────────────────────────────

    async def _filter_image(self, image_bytes: bytes) -> bool:
        """用 LLM Vision 根据筛选提示词判断图片是否可入库。返回 True=通过。"""
        cfg = load_mood_provider(self.context, self.settings)
        if not cfg.valid:
            return True  # 无 API 时跳过筛选

        filter_prompt = self.settings.auto_update_filter_prompt.strip()
        prompt = (
            f"Look at this image and judge whether it meets the following condition:\n"
            f"{filter_prompt}\n\n"
            f"If the image meets the condition, output ONLY: PASS\n"
            f"If the image does NOT meet the condition, output ONLY: REJECT\n"
            f"Output nothing else."
        )

        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        try:
            result = await LLMClient.call(
                cfg, prompt,
                b64_image=b64_data,
                max_tokens=30,
                timeout=self.settings.llm_timeout,
                single_line=True,
            )
            if not result:
                return True  # API 失败时放行

            raw = result.strip()
            # 提取第一个有效词：只看是否以 PASS 或 REJECT 开头
            first_word = raw.lower().split()[0] if raw else ""
            passed = first_word.startswith("pass")

            if not passed:
                logger.info(f"[MemeMemPlus] 筛选 LLM 原始回复: '{raw}'")

            return passed
        except Exception as e:
            logger.warning(f"[MemeMemPlus] 筛选异常: {e}")
            return True

    async def _classify_mood(
        self, image_bytes: bytes, available_moods: list[str], post_url: str = ""
    ) -> str | None:
        """用 LLM Vision 分析图片最接近哪个心情标签。"""
        cfg = load_mood_provider(self.context, self.settings)
        if not cfg.valid:
            logger.warning("[MemeMemPlus] 自动更新: 无法加载情绪分析 API")
            return None

        moods_str = ", ".join(available_moods)
        prompt = (
            f"Look at this image and classify the overall mood/emotion into exactly ONE of these categories: {moods_str}\n"
            f"Focus on the dominant character's expression. If multiple characters, use the main one.\n"
            f"Output ONLY: NONE if the image has no character at all (e.g. pure landscape, object, text).\n"
            f"Otherwise you MUST pick the closest category. Output ONLY the category name, nothing else."
        )

        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        try:
            result = await LLMClient.call(
                cfg, prompt,
                b64_image=b64_data,
                max_tokens=30,
                timeout=self.settings.llm_timeout,
                single_line=True,
            )
            if not result:
                return None
            return self._match_mood(result, available_moods, post_url=post_url)
        except Exception as e:
            logger.error(f"[MemeMemPlus] 自动更新分类失败: {type(e).__name__}: {e}")
            return "__api_error__"  # 区分 API 失败和 LLM 明确返回无法分类

    @staticmethod
    def _match_mood(text: str, available_moods: list[str], post_url: str = "") -> str | None:
        """将 LLM 输出匹配到可用心情列表。"""
        text = text.strip().lower().strip(".,!?;:\"'()[]{}*")
        if text == "none":
            logger.info(f"[MemeMemPlus] 自动更新: LLM 判断图片不适合归类，跳过 → {post_url}")
            return None
        for mood in available_moods:
            if mood.lower() == text:
                return mood
        # 模糊匹配：LLM 输出包含心情名
        for mood in available_moods:
            if mood.lower() in text:
                return mood
        logger.info(f"[MemeMemPlus] 自动更新: LLM 输出 '{text}' 无法匹配心情 → {post_url}")
        return None

    # ── 保存 ──────────────────────────────────────────────────

    async def _save_image(self, mood: str, image_bytes: bytes, post_id: str) -> None:
        """保存图片到心情目录，标记为 booru 来源。"""
        mood_dir = self.library_mgr.library_dir / mood
        mood_dir.mkdir(parents=True, exist_ok=True)

        name_hash = hashlib.md5(image_bytes).hexdigest()[:12]
        save_path = mood_dir / f"booru_{post_id}_{name_hash}.jpg"

        if save_path.exists():
            return

        save_path.write_bytes(image_bytes)
        async with self._seen_lock:
            self._seen_ids.add(post_id)
            # 防止 _seen_ids 无限增长：超限时从磁盘重建，但保留内存中的新 ID
            if len(self._seen_ids) > self._MAX_SEEN_IDS:
                disk_ids = self._load_seen_ids()
                self._seen_ids |= disk_ids  # 合并而非替换，避免丢失未持久化的 ID
                # 若合并后仍超限，保留最近的磁盘 ID（文件名中有 post_id，磁盘扫描天然去重）
                if len(self._seen_ids) > self._MAX_SEEN_IDS * 2:
                    self._seen_ids = disk_ids
        logger.debug(f"[MemeMemPlus] 自动更新: 已保存 {save_path.name} → {mood}/")
