from __future__ import annotations

import io

import httpx
import pytest

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
            return httpx.Response(200, json={
                "href": (
                    "https://cloud-api.yandex.net/v1/disk/resources"
                    "?path=disk%3A%2FamoCRM%2F%D0%A1%D0%B4%D0%B5%D0%BB%D0%BA%D0%B8"
                    "%2F31095815%2F31095815_4.zip"
                ),
                "method": "GET",
                "templated": False,
            })
        if request.method == "GET" and request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"public_url": "https://disk.yandex.ru/d/abc"})
        if request.method == "PUT" and request.url.path.endswith("/resources"):
            return httpx.Response(409, json={"error": "DiskPathPointsToExistentDirectoryError"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with YandexDiskClient("secret", timeout_seconds=8, client=http_client) as disk:
            public_url = disk.upload_and_publish(
                disk_path("amoCRM/Сделки", "31095815", "31095815_4.zip"),
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
    metadata_request = next(
        request
        for request in requests
        if request.method == "GET" and request.url.path.endswith("/resources")
    )
    assert metadata_request.url.params["path"].endswith("31095815_4.zip")


def test_disk_root_is_normalized_without_allowing_parent_segments() -> None:
    assert normalize_disk_root(r"/Производство\Цветопробы/") == "Производство/Цветопробы"


def test_yandex_api_error_keeps_safe_provider_code_and_description() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(507, json={
            "error": "DiskNotEnoughSpaceError",
            "description": "Insufficient storage space",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with YandexDiskClient("secret", timeout_seconds=8, client=http_client) as disk:
            with pytest.raises(RuntimeError) as caught:
                disk.account()

    message = str(caught.value)
    assert "HTTP 507" in message
    assert "DiskNotEnoughSpaceError" in message
    assert "Insufficient storage space" in message
    assert "secret" not in message
