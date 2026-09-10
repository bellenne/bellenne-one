from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

YANDEX_DISK_API_URL = "https://cloud-api.yandex.net/v1/disk"
YANDEX_UPLOAD_HOST_SUFFIXES = (".yandex.net", ".yandex.ru", ".yandex.com")
YANDEX_DEALS_ROOT = "amoCRM/Сделки"


def normalize_disk_root(value: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise ValueError("Укажите корневую папку на Яндекс.Диске.")
    parts = PurePosixPath(normalized).parts
    if normalized.casefold().startswith("disk:"):
        raise ValueError("Укажите путь внутри Диска без префикса disk:.")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Корневая папка Яндекс.Диска содержит недопустимый сегмент.")
    return "/".join(parts)


def disk_path(*parts: str) -> str:
    cleaned: list[str] = []
    for part in parts:
        value = part.strip().replace("\\", "/").strip("/")
        if not value:
            continue
        segments = PurePosixPath(value).parts
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("Путь Яндекс.Диска содержит недопустимый сегмент.")
        cleaned.extend(segments)
    if not cleaned:
        raise ValueError("Путь Яндекс.Диска не задан.")
    return "disk:/" + "/".join(cleaned)


class YandexDiskClient:
    def __init__(
        self,
        oauth_token: str,
        *,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        if not oauth_token:
            raise ValueError("OAuth token Яндекс.Диска не настроен.")
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout Яндекс.Диска должен быть положительным.")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self.headers = {"Authorization": f"OAuth {oauth_token}", "Accept": "application/json"}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def account(self) -> dict[str, Any]:
        response = self.client.get(YANDEX_DISK_API_URL, headers=self.headers)
        return self._json(response, "проверка подключения")

    @staticmethod
    def _json(response: httpx.Response, operation: str) -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Яндекс.Диск: {operation} вернул HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Яндекс.Диск: {operation} вернул некорректный ответ.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Яндекс.Диск: {operation} вернул некорректный ответ.")
        return payload

    def ensure_folder(self, path: str) -> None:
        parsed = PurePosixPath(path.removeprefix("disk:/"))
        current: list[str] = []
        for segment in parsed.parts:
            current.append(segment)
            response = self.client.put(
                f"{YANDEX_DISK_API_URL}/resources",
                headers=self.headers,
                params={"path": disk_path(*current)},
            )
            if response.status_code == 409:
                try:
                    error_code = response.json().get("error")
                except ValueError:
                    error_code = None
                if error_code == "DiskPathPointsToExistentDirectoryError":
                    continue
            if response.status_code != 201:
                raise RuntimeError(
                    f"Яндекс.Диск: создание папки вернуло HTTP {response.status_code}."
                )

    @staticmethod
    def _safe_upload_url(value: Any) -> str:
        if not isinstance(value, str):
            raise RuntimeError("Яндекс.Диск не вернул URL загрузки.")
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or not any(hostname.endswith(suffix) for suffix in YANDEX_UPLOAD_HOST_SUFFIXES)
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise RuntimeError("Яндекс.Диск вернул небезопасный URL загрузки.")
        return value

    def upload(self, path: str, file_obj: BinaryIO) -> None:
        response = self.client.get(
            f"{YANDEX_DISK_API_URL}/resources/upload",
            headers=self.headers,
            params={"path": path, "overwrite": "true"},
        )
        payload = self._json(response, "получение URL загрузки")
        upload_url = self._safe_upload_url(payload.get("href"))
        method = str(payload.get("method") or "PUT").upper()
        if method != "PUT":
            raise RuntimeError("Яндекс.Диск вернул неподдерживаемый метод загрузки.")
        upload_response = self.client.put(
            upload_url,
            content=file_obj,
            headers={"Content-Type": "application/zip"},
        )
        if upload_response.status_code not in {201, 202}:
            raise RuntimeError(
                f"Яндекс.Диск: загрузка файла вернула HTTP {upload_response.status_code}."
            )

    def publish(self, path: str) -> str:
        response = self.client.put(
            f"{YANDEX_DISK_API_URL}/resources/publish",
            headers=self.headers,
            params={"path": path},
        )
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(
                f"Яндекс.Диск: публикация файла вернула HTTP {response.status_code}."
            )
        metadata = self.client.get(
            f"{YANDEX_DISK_API_URL}/resources",
            headers=self.headers,
            params={"path": path, "fields": "public_url"},
        )
        payload = self._json(metadata, "получение публичной ссылки")
        public_url = payload.get("public_url")
        if not isinstance(public_url, str) or not public_url.startswith("https://"):
            raise RuntimeError("Яндекс.Диск не вернул публичную ссылку на файл.")
        return public_url

    def upload_and_publish(self, path: str, file_obj: BinaryIO) -> str:
        parent = str(PurePosixPath(path.removeprefix("disk:/")).parent)
        self.ensure_folder(disk_path(parent))
        self.upload(path, file_obj)
        return self.publish(path)
