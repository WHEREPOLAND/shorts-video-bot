from __future__ import annotations

import logging

from aiohttp import web

from .context import AppContext

log = logging.getLogger(__name__)


def _status_payload(app: AppContext) -> dict:
    return {
        "status": "ok",
        "uptime_sec": app.uptime_sec,
        "active": app.active,
        "pending": app.pending,
        "done": app.done,
        "failed": app.failed,
    }


def build_health_app(app: AppContext) -> web.Application:
    async def ping(_: web.Request) -> web.Response:
        return web.json_response(_status_payload(app))

    web_app = web.Application()
    web_app.router.add_get("/", ping)
    web_app.router.add_get("/ping", ping)
    web_app.router.add_get("/health", ping)
    return web_app


async def start_health_server(port: int, app: AppContext) -> web.AppRunner:
    runner = web.AppRunner(build_health_app(app), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health server on 0.0.0.0:%s", port)
    return runner
