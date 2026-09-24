import hashlib
import io
import ipaddress
import os
import socket
import uuid
import warnings
from urllib.parse import urlsplit

import httpx
from PIL import Image

from . import db, policy
from .scenarios import Invalid


ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def is_ozon_host(host):
    host = host.lower().rstrip(".")
    return any(host == root or host.endswith(f".{root}") for root in policy.OZON_MEDIA_ROOTS)


def validate_image(content, settings):
    maximum = policy.IMAGE_MAX_BYTES
    if not content or len(content) > maximum:
        raise Invalid("Файл пуст или превышает допустимый размер")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as im:
                mime = Image.MIME.get(im.format)
                im.verify()
            with Image.open(io.BytesIO(content)) as im:
                im.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise Invalid("Повреждённое или неподдерживаемое изображение") from None
    if mime not in ALLOWED_IMAGE_MIMES:
        raise Invalid("Тип изображения не разрешён")
    return mime


def store(con, content, settings, chat_id=None, item_id=None):
    mime = validate_image(content, settings)
    digest = hashlib.sha256(content).hexdigest()
    mid = uuid.uuid4().hex
    directory = db.DATA / "media"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / mid
    path.write_bytes(content)
    path.chmod(0o600)
    con.execute(
        "INSERT INTO media VALUES(?,?,?,?,?,?,?)",
        (mid, chat_id, item_id, mime, len(content), digest, db.now()),
    )
    return mid


def download(url, settings):
    parsed = urlsplit(url)
    # Only official Ozon DNS namespaces; redirects and private addresses remain
    # forbidden so the convenient default does not weaken SSRF protection.
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise Invalid("Недопустимая ссылка на вложение")
    if not is_ozon_host(host):
        raise Invalid("Ссылка на вложение ведёт не на хранилище Ozon")
    if any(
        not ipaddress.ip_address(item[4][0]).is_global
        for item in socket.getaddrinfo(host, 443)
    ):
        raise Invalid("Недопустимый адрес хранилища")
    maximum = policy.IMAGE_MAX_BYTES
    data = bytearray()
    request_options = {"follow_redirects": False}
    configured_timeout = os.environ.get("FOLIO_OZON_HTTP_TIMEOUT_SECONDS", "").strip()
    if configured_timeout:
        request_options["timeout"] = float(configured_timeout)
    with httpx.stream("GET", url, **request_options) as response:
        if response.status_code != 200:
            raise Invalid("Вложение недоступно")
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise Invalid("Вложение превышает допустимый размер")
    validate_image(bytes(data), settings)
    return bytes(data)
