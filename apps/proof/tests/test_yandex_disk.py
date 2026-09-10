from __future__ import annotations

import io

import httpx

from app.yandex_disk import YandexDiskClient, disk_path, normalize_disk_root


def test_upload_and_publish_creates_folders_and_returns_file_link() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/resources/upload"):
            return httpx.Response(200, json={
                "href": "https://uploader.disk.yandex.net/upload/target",
                "method": "PUT",
            })
        if request.url.host == "uploader.disk.yandex.net":
            return httpx.Response(201)
        if request.url.path.endswith("/resources/publish"):
            return httpx.Response(200, json={})
        if request.method == "GET" and request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"public_url": "https://disk.yandex.ru/d/abc"})
        if request.method == "PUT" and request.url.path.endswith("/resources"):
            return httpx.Response(409, json={"error": "DiskPathPointsToExistentDirectoryError"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with YandexDiskClient("secret", timeout_seconds=8, client=http_client) as disk:
            public_url = disk.upload_and_publish(
                disk_path("amoCRM/Сделки", "31095815", "31095815 4.zip"),
                io.BytesIO(b"archive"),
            )

    assert public_url == "https://disk.yandex.ru/d/abc"
    folder_paths = [
        request.url.params["path"]
        for request in requests
        if request.method == "PUT" and request.url.path.endswith("/resources")
    ]
    assert folder_paths == [
        "disk:/amoCRM",
        "disk:/amoCRM/Сделки",
        "disk:/amoCRM/Сделки/31095815",
    ]
    upload = next(request for request in requests if request.url.host == "uploader.disk.yandex.net")
    assert upload.content == b"archive"


def test_disk_root_is_normalized_without_allowing_parent_segments() -> None:
    assert normalize_disk_root(r"/Производство\Цветопробы/") == "Производство/Цветопробы"
