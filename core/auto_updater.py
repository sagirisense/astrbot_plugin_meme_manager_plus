"""自动图库更新器：定时从 Booru 图站搜索图片，LLM 分类心情后入库。"""

import asyncio
import base64
import hashlib
import random
import traceback
from pathlib import Path

import aiohttp

from astrbot.api import logger

from ..config.settings import PluginSettings
from ..utils.provider_helper import load_mood_provider, LLMApiConfig

# Booru API 端点
BOORU_APIS = {
    "yandere": "https://yande.re/post.json",
    "konachan": "https://konachan.com/post.json",
    "danbooru": "https://danbooru.donmai.us/posts.json",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class AutoUpdater:
    """定时从 Booru 搜索角色图片，用 LLM 判断心情后保存到对应目录。"""

    def __init__(self, settings: PluginSettings, context, library_mgr):
        self.settings = settings
        self.context = context
        self.library_mgr = library_mgr
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # 从磁盘已有文件恢复已下载 ID，避免重复搜索
        self._seen_ids: set[str] = self._load_seen_ids()

    def _load_seen_ids(self) -> set[str]:
        """扫描图库中 booru_ 开头的文件，提取 post ID。"""
        ids = set()
        lib_dir = self.library_mgr.library_dir
        if not lib_dir.exists():
            return ids
        for mood_dir in lib_dir.iterdir():
            if not mood_dir.is_dir():
                continue
            for f in mood_dir.iterdir():
                if f.is_file() and f.name.startswith("booru_"):
                    # 文件名格式: booru_{post_id}_{hash}.jpg
                    parts = f.stem.split("_")
                    if len(parts) >= 2:
                        ids.add(parts[1])
        if ids:
            logger.info(f"[MemeMemPlus] 从磁盘恢复 {len(ids)} 个已下载图片 ID")
        return ids

    def start(self) -> None:
        """启动后台定时任务。"""
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[MemeMemPlus] 自动更新已启动: "
            f"间隔={self.settings.auto_update_interval_hours}h, "
            f"标签={self.settings.auto_update_search_tags}, "
            f"来源={self.settings.auto_update_source}"
        )

    def stop(self) -> None:
        """停止后台任务。"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("[MemeMemPlus] 自动更新已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        """主循环：等待间隔 → 执行一轮搜图。"""
        interval = max(0.5, self.settings.auto_update_interval_hours) * 3600
        # 首次启动延迟 30 秒，避免和初始化抢资源
        await self._wait(30)

        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except Exception:
                logger.error(f"[MemeMemPlus] 自动更新异常: {traceback.format_exc()}")
            await self._wait(interval)

    async def _wait(self, seconds: float) -> None:
        """可中断的等待。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_once(self, limit_override: int | None = None) -> int:
        """执行一轮：搜索 → 下载 → LLM 分类 → 保存。返回入库数量。"""
        logger.info("[MemeMemPlus] 自动更新: 开始搜索图片...")

        posts = await self._search_images(limit_override=limit_override)
        if not posts:
            logger.info("[MemeMemPlus] 自动更新: 未找到新图片")
            return 0

        available_moods = self.library_mgr.get_all_moods()
        if not available_moods:
            logger.warning("[MemeMemPlus] 自动更新: 无可用心情标签")
            return 0

        saved = 0
        skipped_dl = 0
        skipped_mood = 0
        skipped_filter = 0
        for post in posts:
            if self._stop_event.is_set():
                break
            try:
                image_bytes = await self._download_image(post["url"])
                if not image_bytes:
                    skipped_dl += 1
                    continue

                # 筛选：如果配置了筛选提示词，先让 LLM 判断是否入库
                if self.settings.auto_update_filter_prompt:
                    if not await self._filter_image(image_bytes):
                        skipped_filter += 1
                        self._seen_ids.add(post["id"])
                        logger.info(f"[MemeMemPlus] 自动更新: 图片 {post['id']} 被筛选拒绝")
                        continue

                mood = await self._classify_mood(image_bytes, available_moods)
                if not mood:
                    skipped_mood += 1
                    continue

                self._save_image(mood, image_bytes, post["id"])
                saved += 1
                logger.info(
                    f"[MemeMemPlus] 自动更新: 图片 {post['id']} → {mood}"
                )
            except Exception:
                logger.warning(
                    f"[MemeMemPlus] 自动更新: 处理图片 {post.get('id', '?')} 失败: "
                    f"{traceback.format_exc()}"
                )

        self.library_mgr.refresh()
        logger.info(
            f"[MemeMemPlus] 自动更新完成: 搜索 {len(posts)} 张, "
            f"入库 {saved} 张, 下载失败 {skipped_dl}, 筛选拒绝 {skipped_filter}, 分类失败 {skipped_mood}"
        )
        return saved

    # ── 搜索 ──────────────────────────────────────────────────

    async def _search_images(self, limit_override: int | None = None) -> list[dict]:
        """从 Booru 搜索图片，返回 [{id, url}, ...]。"""
        source = self.settings.auto_update_source.lower()
        tags = self.settings.auto_update_search_tags.strip()
        limit = min(max(1, limit_override or self.settings.auto_update_images_per_cycle), 50)
        min_score = self.settings.auto_update_min_score

        logger.info(
            f"[MemeMemPlus] 搜索参数: source={source}, tags='{tags}', "
            f"limit={limit}, min_score={min_score}"
        )

        api_url = BOORU_APIS.get(source)
        if not api_url:
            logger.error(f"[MemeMemPlus] 未知图片来源: {source}")
            return []

        per_page = limit * 5  # 多取一些，补偿已下载的重复帖子

        # 先请求第 1 页探测总量，再随机翻页
        page = 1
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 第一次请求：探测 + 取数据
                params = {"tags": tags, "limit": per_page, "page": 1}
                async with session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"[MemeMemPlus] Booru API 错误 {resp.status}: "
                            f"{(await resp.text())[:200]}"
                        )
                        return []
                    first_page = await resp.json()

                logger.info(
                    f"[MemeMemPlus] Booru 第1页返回 {len(first_page)} 条, "
                    f"已跳过ID数={len(self._seen_ids)}"
                )
                if first_page and len(first_page) >= per_page:
                    # 有更多页，随机翻页
                    max_page = max(1, 500 // per_page)  # 保守估计
                    page = random.randint(1, max_page)
                    if page > 1:
                        params["page"] = page
                        async with session.get(api_url, params=params) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if data:
                                    first_page = data
                                # 如果随机页为空，回退用第 1 页数据

                data = first_page
        except Exception as e:
            logger.error(f"[MemeMemPlus] Booru 搜索失败: {e}")
            return []

        logger.debug(f"[MemeMemPlus] Booru 搜索: source={source}, tags={tags}, page={page}, 返回 {len(data)} 条")

        results = []
        for post in data:
            post_id = str(post.get("id", ""))
            if post_id in self._seen_ids:
                continue

            # 评分过滤
            score = post.get("score", 0)
            if score < min_score:
                continue

            # 获取图片 URL
            if source == "danbooru":
                url = post.get("file_url") or post.get("large_file_url", "")
            else:
                # yandere / konachan: 优先 jpeg_url（大图），其次 file_url
                url = post.get("jpeg_url") or post.get("file_url", "")

            if not url:
                continue

            # 只要图片格式
            ext = Path(url.split("?")[0]).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue

            results.append({"id": post_id, "url": url})
            if len(results) >= limit:
                break

        random.shuffle(results)
        return results

    # ── 下载 ──────────────────────────────────────────────────

    async def _download_image(self, url: str) -> bytes | None:
        """下载图片，限制最大 10MB。"""
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    # 检查大小
                    size = int(resp.headers.get("Content-Length", 0))
                    if size > 10 * 1024 * 1024:
                        logger.debug(f"[MemeMemPlus] 图片过大跳过: {size // 1024}KB")
                        return None
                    return await resp.read()
        except Exception as e:
            logger.debug(f"[MemeMemPlus] 下载图片失败: {e}")
            return None

    # ── 筛选 ──────────────────────────────────────────────────

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
            if cfg.is_gemini:
                result = await self._call_vision_gemini(cfg, prompt, b64_data)
            else:
                result = await self._call_vision_openai(cfg, prompt, b64_data)

            if not result:
                return True  # API 失败时放行
            return "reject" not in result.strip().lower()
        except Exception as e:
            logger.warning(f"[MemeMemPlus] 筛选异常: {e}")
            return True

    async def _call_vision_gemini(
        self, cfg: LLMApiConfig, prompt: str, b64_data: str
    ) -> str | None:
        """Gemini vision 通用调用，返回文本。"""
        if not cfg.api_base.endswith(("/v1beta", "/v1")):
            url = f"{cfg.api_base}/v1beta/models/{cfg.model}:generateContent"
        else:
            url = f"{cfg.api_base}/models/{cfg.model}:generateContent"

        headers = {"x-goog-api-key": cfg.api_key, "Content-Type": "application/json"}
        payload = {
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": b64_data}},
                {"text": prompt},
            ]}],
            "generationConfig": {"maxOutputTokens": 30},
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return None
                    for part in candidates[0].get("content", {}).get("parts", []):
                        if "text" in part:
                            return part["text"].strip()
        except Exception:
            pass
        return None

    async def _call_vision_openai(
        self, cfg: LLMApiConfig, prompt: str, b64_data: str
    ) -> str | None:
        """OpenAI vision 通用调用，返回文本。"""
        if not cfg.api_base.endswith("/v1"):
            url = f"{cfg.api_base}/v1/chat/completions"
        else:
            url = f"{cfg.api_base}/chat/completions"

        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": 30,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return None
                    return choices[0].get("message", {}).get("content", "").strip()
        except Exception:
            pass
        return None

    async def _classify_mood(
        self, image_bytes: bytes, available_moods: list[str]
    ) -> str | None:
        """用 LLM Vision 分析图片最接近哪个心情标签。"""
        cfg = load_mood_provider(self.context, self.settings)
        if not cfg.valid:
            logger.warning("[MemeMemPlus] 自动更新: 无法加载情绪分析 API")
            return None

        moods_str = ", ".join(available_moods)
        prompt = (
            f"Look at this anime character image. "
            f"Classify the character's expression/mood into exactly ONE of these categories: {moods_str}\n"
            f"Output ONLY the category name, nothing else."
        )

        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        if cfg.is_gemini:
            return await self._classify_gemini(cfg, prompt, b64_data, available_moods)
        else:
            return await self._classify_openai(cfg, prompt, b64_data, available_moods)

    async def _classify_gemini(
        self, cfg: LLMApiConfig,
        prompt: str, b64_data: str, available_moods: list[str]
    ) -> str | None:
        """Gemini vision 分类。"""
        if not cfg.api_base.endswith(("/v1beta", "/v1")):
            url = f"{cfg.api_base}/v1beta/models/{cfg.model}:generateContent"
        else:
            url = f"{cfg.api_base}/models/{cfg.model}:generateContent"

        headers = {"x-goog-api-key": cfg.api_key, "Content-Type": "application/json"}
        payload = {
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": b64_data}},
                {"text": prompt},
            ]}],
            "generationConfig": {"maxOutputTokens": 30},
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"[MemeMemPlus] 自动更新 Gemini 错误 {resp.status}")
                        return None
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return None
                    for part in candidates[0].get("content", {}).get("parts", []):
                        if "text" in part:
                            return self._match_mood(part["text"].strip(), available_moods)
        except Exception as e:
            logger.error(f"[MemeMemPlus] 自动更新分类失败: {e}")
        return None

    async def _classify_openai(
        self, cfg: LLMApiConfig,
        prompt: str, b64_data: str, available_moods: list[str]
    ) -> str | None:
        """OpenAI vision 分类。"""
        if not cfg.api_base.endswith("/v1"):
            url = f"{cfg.api_base}/v1/chat/completions"
        else:
            url = f"{cfg.api_base}/chat/completions"

        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": 30,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"[MemeMemPlus] 自动更新 OpenAI 错误 {resp.status}")
                        return None
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return None
                    content = choices[0].get("message", {}).get("content", "").strip()
                    return self._match_mood(content, available_moods)
        except Exception as e:
            logger.error(f"[MemeMemPlus] 自动更新分类失败: {e}")
        return None

    @staticmethod
    def _match_mood(text: str, available_moods: list[str]) -> str | None:
        """将 LLM 输出匹配到可用心情列表。"""
        text = text.strip().lower().strip(".,!?;:\"'()[]{}*")
        for mood in available_moods:
            if mood.lower() == text:
                return mood
        # 模糊匹配：LLM 输出包含心情名
        for mood in available_moods:
            if mood.lower() in text:
                return mood
        logger.debug(f"[MemeMemPlus] 自动更新: LLM 输出 '{text}' 无法匹配心情")
        return None

    # ── 保存 ──────────────────────────────────────────────────

    def _save_image(self, mood: str, image_bytes: bytes, post_id: str) -> None:
        """保存图片到心情目录，标记为 booru 来源。"""
        mood_dir = self.library_mgr.library_dir / mood
        mood_dir.mkdir(parents=True, exist_ok=True)

        name_hash = hashlib.md5(image_bytes).hexdigest()[:10]
        save_path = mood_dir / f"booru_{post_id}_{name_hash}.jpg"

        if save_path.exists():
            return

        save_path.write_bytes(image_bytes)
        self._seen_ids.add(post_id)
        logger.debug(f"[MemeMemPlus] 自动更新: 已保存 {save_path.name} → {mood}/")
