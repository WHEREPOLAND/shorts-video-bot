from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import Config


@dataclass(slots=True)
class AppContext:
    cfg: Config
    semaphore: asyncio.Semaphore
    started_at: float = field(default_factory=time.time)
    active: int = 0
    pending: int = 0
    done: int = 0
    failed: int = 0
    tasks: set = field(default_factory=set)

    @property
    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at)
