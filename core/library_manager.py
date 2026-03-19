import hashlib
import random
from pathlib import Path

from astrbot.api import logger

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

DEFAULT_MOODS = [
    "angry", "baka", "color", "confused", "cpu",
    "fool", "givemoney", "happy", "like", "meow",
    "morning", "reply", "sad", "see", "shy",
    "sigh", "sleep", "surprised", "work",
]


class LibraryManager:
    """管理按心情分类的参考图片库。

    用户可在 library/ 下自由添加文件夹来扩展心情标签，
    插件自动扫描识别所有子目录名作为可用心情。
    """

    def __init__(self, library_dir: Path):
        self.library_dir = library_dir
        self._cache: dict[str, list[Path]] = {}
        self._hash_index: dict[str, Path] = {}  # md5 → file path, 惰性构建
        self._hash_index_built = False
        self._initialized = False

    def initialize(self) -> None:
        """创建预设心情目录，扫描所有子目录中的图片。"""
        self.library_dir.mkdir(parents=True, exist_ok=True)

        # 创建预设心情目录（不影响用户已创建的自定义目录）
        for mood in DEFAULT_MOODS:
            mood_dir = self.library_dir / mood
            mood_dir.mkdir(exist_ok=True)

        self.refresh()
        self._initialized = True

    def refresh(self) -> None:
        """重新扫描图库，更新缓存。"""
        self._cache.clear()
        self._hash_index.clear()
        self._hash_index_built = False
        if not self.library_dir.exists():
            return

        for mood_dir in self.library_dir.iterdir():
            if not mood_dir.is_dir():
                continue
            mood_name = mood_dir.name
            # 跳过隐藏目录和 Python 缓存目录
            if mood_name.startswith((".", "__")):
                continue
            images = [
                f for f in mood_dir.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
            ]
            self._cache[mood_name] = images

        stats = self.get_stats()
        total = sum(stats.values())
        non_empty = sum(1 for v in stats.values() if v > 0)
        logger.info(
            f"[MemeMemPlus] 图库扫描完成: {len(stats)} 个心情, "
            f"{non_empty} 个有参考图, 共 {total} 张图片"
        )

    def get_all_moods(self) -> list[str]:
        """返回所有可用心情标签（来自目录扫描，含自定义）。"""
        if not self._initialized:
            self.refresh()
        return list(self._cache.keys())

    def get_random_reference(self, mood: str) -> Path | None:
        """从指定心情目录随机取一张参考图。目录为空返回 None。"""
        images = self._cache.get(mood, [])
        if not images:
            return None
        return random.choice(images)

    def get_all_references(self, mood: str) -> list[Path]:
        """返回指定心情目录下的所有参考图。"""
        return list(self._cache.get(mood, []))

    def has_reference(self, mood: str) -> bool:
        """判断该心情是否有参考图片。"""
        return bool(self._cache.get(mood))

    def get_stats(self) -> dict[str, int]:
        """返回各心情目录的图片数量统计。"""
        return {mood: len(images) for mood, images in self._cache.items()}

    def _build_hash_index(self) -> None:
        """构建 MD5 → 文件路径的索引（首次调用 find_by_hash 时触发）。"""
        self._hash_index.clear()
        for images in self._cache.values():
            for img_path in images:
                try:
                    h = hashlib.md5(img_path.read_bytes()).hexdigest()
                    self._hash_index[h] = img_path
                except Exception as e:
                    logger.debug(f"[MemeMemPlus] 哈希计算失败，跳过: {img_path.name}: {e}")
                    continue
        self._hash_index_built = True
        logger.info(f"[MemeMemPlus] 哈希索引已构建: {len(self._hash_index)} 张图片")

    def find_by_hash(self, image_bytes: bytes) -> Path | None:
        """通过 MD5 哈希在图库中查找匹配的图片文件。"""
        if not self._hash_index_built:
            self._build_hash_index()
        target_hash = hashlib.md5(image_bytes).hexdigest()
        found = self._hash_index.get(target_hash)
        # 验证文件仍然存在（可能被手动删除）
        if found and found.exists():
            return found
        return None
