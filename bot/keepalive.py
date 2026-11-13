from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)


async def keepalive_loop(url: str, interval_sec: int) -> None:
    """Пингует собственный /ping, чтобы free-инстанс Render не засыпал.

    Render считает входящим любой HTTP-запрос через публичный URL, поэтому
    запрос «самого себя» продлевает жизнь сервиса.
    """
    target = url.rstrip("/") + "/ping"
    interval = max(60, interval_sec)
    timeout = aiohttp.ClientTimeout(total=30)
    log.info("keepalive every %ss -> %s", interval, target)
    await asyncio.sleep(min(interval, 120))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(target) as response:
                    await response.read()
                    log.debug("keepalive: %s", response.status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("keepalive failed: %s", exc)
            await asyncio.sleep(interval)
