"""Публикация проекта на GitHub без git (через GitHub REST API).

Использование:
    python tools/publish.py --env-file .deploy.env
    python tools/publish.py --env-file .deploy.env --message "update bot"

Загружает все файлы проекта в ветку main отдельными коммитами (Contents API).
Токену нужны права Contents: write; для создания репозитория — Administration: write.
Файлы .github/workflows требуют отдельного права Workflows: write — без него они пропускаются.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".idea", ".vscode", "node_modules"}
SKIP_FILES = {".env", ".deploy.env", "cookies.txt"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".session", ".zip"}

API = "https://api.github.com"


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def api(token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None
    for attempt in range(4):
        request = urllib.request.Request(f"{API}{path}", data=body, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "shorts-video-bot-publisher")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(2 + attempt * 2)
    raise SystemExit(f"GitHub: сеть недоступна после нескольких попыток: {last_error}")


def collect_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def ensure_repo(token: str, owner: str, repo: str) -> None:
    status, data = api(token, "GET", f"/repos/{owner}/{repo}")
    if status == 200:
        print(f"repo exists: {owner}/{repo}")
        return
    if status != 404:
        raise SystemExit(f"GitHub: не удалось проверить репозиторий ({status}): {data}")

    status, data = api(
        token,
        "POST",
        "/user/repos",
        {
            "name": repo,
            "description": "Telegram bot: Shorts / Reels / TikTok -> video in chat",
            "private": False,
            "auto_init": False,
            "has_issues": True,
            "has_wiki": False,
        },
    )
    if status not in (200, 201):
        raise SystemExit(f"GitHub: не удалось создать репозиторий ({status}): {data}")
    print(f"repo created: {data.get('full_name')}")


def blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()


def put_file(token: str, owner: str, repo: str, path: Path, message: str) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    encoded_path = urllib.parse.quote(relative, safe="/")
    content = path.read_bytes()
    payload: dict = {
        "message": f"{message}: {relative}",
        "content": base64.b64encode(content).decode("ascii"),
    }
    status, existing = api(token, "GET", f"/repos/{owner}/{repo}/contents/{encoded_path}")
    if status == 200 and isinstance(existing, dict):
        if existing.get("sha") == blob_sha(content):
            print(f"  = {relative}")
            return True
        if existing.get("sha"):
            payload["sha"] = existing["sha"]

    status, data = api(token, "PUT", f"/repos/{owner}/{repo}/contents/{encoded_path}", payload)
    if status in (200, 201):
        print(f"  + {relative}")
        return True
    message_text = data.get("message") if isinstance(data, dict) else str(data)
    print(f"  ! {relative}: {status} {message_text}")
    return False


def publish(token: str, owner: str, repo: str, message: str) -> None:
    ensure_repo(token, owner, repo)
    files = collect_files()
    if not files:
        raise SystemExit("Нет файлов для публикации.")

    uploaded = 0
    skipped: list[str] = []
    for path in files:
        if put_file(token, owner, repo, path, message):
            uploaded += 1
        else:
            skipped.append(path.relative_to(ROOT).as_posix())

    print(f"published {uploaded} files -> https://github.com/{owner}/{repo}")
    if skipped:
        print("skipped (нужны доп. права токена): " + ", ".join(skipped))


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload project to GitHub via REST API")
    parser.add_argument("--env-file", default=str(ROOT / ".deploy.env"))
    parser.add_argument("--repo", help="owner/repo (по умолчанию из GITHUB_REPO)")
    parser.add_argument("--message", default="Update shorts-video-bot")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.is_file():
        raise SystemExit(f"Не найден env-файл: {env_path}")
    env = load_env(env_path)
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN не задан в env-файле.")

    repo_full = args.repo or env.get("GITHUB_REPO", "")
    if "/" not in repo_full:
        raise SystemExit("Укажите GITHUB_REPO=owner/repo или --repo.")
    owner, repo = repo_full.split("/", 1)
    publish(token, owner, repo, args.message)


if __name__ == "__main__":
    sys.exit(main())
