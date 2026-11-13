"""Создание/обновление сервиса на Render через REST API.

Запуск (нужен доступ к api.render.com — VPN/прокси):

    python tools/render_deploy.py

Переменные окружения (можно взять из .deploy.env):
    RENDER_API_KEY     — ключ Render (обязательно)
    RENDER_SERVICE_ID  — ID существующего сервиса (необязательно; иначе поиск по имени)
    TELEGRAM_TOKEN     — токен бота (передаётся в Render как env var)
    ADMIN_IDS          — ID админов (необязательно)
    RENDER_REPO        — URL репозитория (по умолчанию github.com/WHEREPOLAND/shorts-video-bot)
    RENDER_NAME        — имя сервиса (по умолчанию shorts-video-bot)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.render.com/v1"
NAME = os.getenv("RENDER_NAME", "shorts-video-bot")
REPO = os.getenv("RENDER_REPO", "https://github.com/WHEREPOLAND/shorts-video-bot")
BRANCH = os.getenv("RENDER_BRANCH", "main")
REGION = os.getenv("RENDER_REGION", "frankfurt")
PLAN = os.getenv("RENDER_PLAN", "free")

ENV_VARS = [
    {"key": "TELEGRAM_TOKEN", "value": os.getenv("TELEGRAM_TOKEN", "")},
    {"key": "ADMIN_IDS", "value": os.getenv("ADMIN_IDS", "")},
    {"key": "MAX_UPLOAD_MB", "value": "48"},
    {"key": "MAX_DOWNLOAD_MB", "value": "512"},
    {"key": "MAX_DURATION_SEC", "value": "600"},
    {"key": "MAX_CONCURRENT_JOBS", "value": "1"},
    {"key": "MAX_QUEUE", "value": "10"},
    {"key": "MAX_LINKS_PER_MESSAGE", "value": "3"},
    {"key": "FFMPEG_PRESET", "value": "ultrafast"},
    {"key": "WORK_DIR", "value": "/tmp/shorts-video-bot"},
    {"key": "LOG_LEVEL", "value": "INFO"},
    {"key": "PYTHONUNBUFFERED", "value": "1"},
]
ENV_VARS = [item for item in ENV_VARS if item["value"]]


def api(key: str, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=body, method=method)
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def pick_owner(key: str, service: dict | None) -> str:
    if service and service.get("ownerId"):
        return service["ownerId"]
    status, data = api(key, "GET", "/owners?limit=50")
    if status != 200 or not isinstance(data, list) or not data:
        raise SystemExit(f"Render: не удалось получить owners ({status}): {data}")
    for item in data:
        owner = item.get("owner", item)
        if owner.get("type") == "user":
            return owner["id"]
    owner = data[0].get("owner", data[0])
    return owner["id"]


def find_service(key: str) -> dict | None:
    service_id = os.getenv("RENDER_SERVICE_ID", "").strip()
    if service_id:
        status, data = api(key, "GET", f"/services/{service_id}")
        if status == 200 and isinstance(data, dict):
            print(f"render service: {data.get('name')} ({service_id})")
            return data
    status, data = api(key, "GET", f"/services?name={NAME}&limit=20")
    if status == 200 and isinstance(data, list):
        for item in data:
            service = item.get("service", item)
            if service.get("name") == NAME:
                print(f"render service found: {service.get('name')} ({service.get('id')})")
                return service
    return None


def create_service(key: str, owner_id: str) -> dict:
    payload = {
        "type": "web_service",
        "name": NAME,
        "ownerId": owner_id,
        "repo": REPO,
        "branch": BRANCH,
        "autoDeploy": "yes",
        "serviceDetails": {
            "env": "docker",
            "plan": PLAN,
            "region": REGION,
            "healthCheckPath": "/ping",
            "envSpecificDetails": {
                "dockerCommand": "",
                "dockerContext": ".",
                "dockerfilePath": "./Dockerfile",
            },
        },
        "envVars": ENV_VARS,
    }
    status, data = api(key, "POST", "/services", payload)
    if status not in (200, 201):
        raise SystemExit(f"Render: сервис не создался ({status}): {data}")
    service = data.get("service", data)
    print(f"render service created: {service.get('id')} ({service.get('serviceDetails', {}).get('url', '')})")
    return service


def update_service(key: str, service: dict) -> None:
    service_id = service["id"]
    status, data = api(key, "PUT", f"/services/{service_id}/env-vars", ENV_VARS)
    if status not in (200, 201):
        print(f"warning: env vars not updated ({status}): {data}")
    else:
        print("env vars updated")

    details = service.get("serviceDetails") or {}
    if details.get("plan") != PLAN or details.get("region") != REGION:
        status, data = api(key, "PATCH", f"/services/{service_id}", {"serviceDetails": {"plan": PLAN}})
        if status not in (200, 201):
            print(f"warning: plan update failed ({status}): {data}")

    status, data = api(key, "POST", f"/services/{service_id}/deploys", {})
    if status not in (200, 201):
        raise SystemExit(f"Render: деплой не запустился ({status}): {data}")
    deploy = data if isinstance(data, dict) else {}
    print(f"deploy triggered: {deploy.get('id', '')} status={deploy.get('status', '')}")


def main() -> None:
    key = os.getenv("RENDER_API_KEY", "").strip()
    if not key:
        raise SystemExit("RENDER_API_KEY не задан.")

    service = find_service(key)
    if service:
        update_service(key, service)
    else:
        owner_id = pick_owner(key, service)
        created = create_service(key, owner_id)
        service_id = created.get("id")
        if service_id:
            status, data = api(key, "GET", f"/services/{service_id}")
            if status == 200 and isinstance(data, dict):
                print(f"dashboard: https://dashboard.render.com/web/{service_id}")
                details = data.get("serviceDetails") or {}
                if details.get("url"):
                    print(f"url: {details['url']}")
    print("done")


if __name__ == "__main__":
    sys.exit(main())
