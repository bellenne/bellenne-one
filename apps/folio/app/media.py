import hashlib
import io
import ipaddress
import socket
import uuid
import warnings
from urllib.parse import urlsplit

import httpx
from PIL import Image

from . import db
from .scenarios import Invalid


def validate_image(content, settings):
    maximum = settings.get("media_max_bytes")
    allowed = settings.get("media_mimes", [])
    if not maximum or not allowed:
        raise Invalid("Администратор должен настроить типы и размер изображений")
    if not content or len(content) > maximum:
        raise Invalid("Файл пуст или превышает настроенный размер")
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
    if mime not in allowed or mime not in {"image/jpeg", "image/png", "image/webp"}:
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
    # Only the official CDN namespace, additionally selected by the administrator.
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise Invalid("Недопустимая ссылка на вложение")
    if host not in settings.get("media_hosts", []) or not (
        host.endswith((".ozone.ru", ".ozon.ru"))
    ):
        raise Invalid("Хост вложения не подтверждён в настройках")
    if any(
        not ipaddress.ip_address(item[4][0]).is_global
        for item in socket.getaddrinfo(host, 443)
    ):
        raise Invalid("Недопустимый адрес хранилища")
    maximum = settings.get("media_max_bytes")
    if not maximum:
        raise Invalid("Не настроен размер вложений")
    data = bytearray()
    with httpx.stream(
        "GET", url, timeout=settings["http_timeout"], follow_redirects=False
    ) as response:
        if response.status_code != 200:
            raise Invalid("Вложение недоступно")
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise Invalid("Вложение превышает допустимый размер")
    validate_image(bytes(data), settings)
    return bytes(data)
