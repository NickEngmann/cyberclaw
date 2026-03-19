"""Mission timer and runtime enforcement."""

import time


class Watchdog:
    """Tracks mission runtime and triggers warnings/expiry."""

    def __init__(self, max_hours: float):
        self.max_seconds = max_hours * 3600
        self.start_time = None

    def start(self):
        self.start_time = time.time()

    def check(self) -> str:
        """Returns status: 'ok', 'warn_75', 'warn_90', or 'expired'."""
        if self.start_time is None:
            return "ok"
        elapsed = time.time() - self.start_time
        pct = elapsed / self.max_seconds
        if pct >= 1.0:
            return "expired"
        elif pct >= 0.90:
            return "warn_90"
        elif pct >= 0.75:
            return "warn_75"
        return "ok"

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    def remaining(self) -> float:
        return max(0, self.max_seconds - self.elapsed())

    def format_elapsed(self) -> str:
        return self._fmt(self.elapsed())

    def format_remaining(self) -> str:
        return self._fmt(self.remaining())

    @staticmethod
    def _fmt(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
