import time


class CooldownManager:
    """冷却时间管理，防止过于频繁地生成图片。"""

    MAX_ENTRIES = 500  # 最大记录数，超过时触发清理

    def __init__(self, cooldown_seconds: int, per_group: bool):
        self.cooldown_seconds = cooldown_seconds
        self.per_group = per_group
        self._last_trigger: dict[str, float] = {}

    def _get_key(self, session_id: str, group_id: str | None) -> str:
        if self.per_group and group_id:
            return f"group:{group_id}"
        return f"session:{session_id}"

    def _cleanup(self) -> None:
        """清理过期记录，防止内存泄漏。"""
        if len(self._last_trigger) <= self.MAX_ENTRIES:
            return
        now = time.time()
        expired = [
            k for k, v in self._last_trigger.items()
            if (now - v) > self.cooldown_seconds * 2
        ]
        for k in expired:
            del self._last_trigger[k]

    def can_trigger(self, session_id: str, group_id: str | None = None) -> bool:
        """检查是否已过冷却期。"""
        if self.cooldown_seconds <= 0:
            return True
        key = self._get_key(session_id, group_id)
        last = self._last_trigger.get(key, 0)
        return (time.time() - last) >= self.cooldown_seconds

    def record(self, session_id: str, group_id: str | None = None) -> None:
        """记录触发时间。"""
        key = self._get_key(session_id, group_id)
        self._last_trigger[key] = time.time()
        self._cleanup()
