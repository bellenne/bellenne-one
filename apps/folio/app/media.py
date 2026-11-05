import hashlib
import io
import ipaddress
import os
import socket
import time
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


def download(url, settings, auth_client=None):
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
    if host == "api-seller.ozon.ru":
        # Chat history uses an authenticated file endpoint. Never send the
        # cabinet's API credentials to an arbitrary URL, even under *.ozon.ru.
        if not parsed.path.startswith("/v2/chat/file/") or auth_client is None:
            raise Invalid("Недопустимый адрес файла Ozon")
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
    stream = (
        auth_client.stream("GET", url, **request_options)
        if host == "api-seller.ozon.ru"
        else httpx.stream("GET", url, **request_options)
    )
    with stream as response:
        if response.status_code != 200:
            raise Invalid("Вложение недоступно")
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise Invalid("Вложение превышает допустимый размер")
    validate_image(bytes(data), settings)
    return bytes(data)


def _public_target(url):
    text = str(url or "").strip()
    message = "Проверьте ссылку: нужен полный адрес изображения, начинающийся с http:// или https://"
    if not text or len(text) > 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in text):
        raise Invalid(message)
    try:
        target = httpx.URL(text)
    except (httpx.InvalidURL, ValueError):
        raise Invalid(message) from None
    if (target.scheme not in {"http", "https"} or not target.host or target.userinfo
            or target.port not in (None, 80, 443)):
        raise Invalid(message)
    return target


def _public_addresses(target):
    # Every address must be public, otherwise a link could reach our own network.
    host = target.raw_host.decode("ascii")
    try:
        found = socket.getaddrinfo(host, target.port or (443 if target.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise Invalid("Сайт из ссылки не найден. Проверьте адрес") from None
    addresses = list(dict.fromkeys(item[4][0] for item in found))
    for address in addresses:
        parsed = ipaddress.ip_address(address.split("%")[0])
        parsed = getattr(parsed, "ipv4_mapped", None) or parsed
        if not parsed.is_global or parsed.is_multicast or parsed.is_reserved:
            raise Invalid("Ссылка ведёт на внутренний адрес. Такие изображения загружать нельзя")
    if not addresses:
        raise Invalid("Сайт из ссылки не найден. Проверьте адрес")
    return sorted(addresses, key=lambda address: ":" in address)


def _read_public(client, target, address, deadline):
    """Return (content, None), or (None, next URL) for a redirect."""
    # The request goes to the checked address itself: a second DNS answer cannot
    # redirect it. TLS still verifies the certificate against the original name.
    request = {"headers": {"Host": target.netloc.decode("ascii"), "Accept": "image/*",
                           "User-Agent": "Mozilla/5.0 (compatible; BellenneFolio)"},
               "extensions": {"sni_hostname": target.raw_host.decode("ascii")}}
    with client.stream("GET", target.copy_with(host=address), **request) as response:
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise Invalid("Сайт вернул некорректное перенаправление. Скачайте изображение и загрузите его файлом")
            return None, location
        if response.status_code != 200:
            raise Invalid(f"Сайт не отдал файл (код {response.status_code}). Скачайте изображение и загрузите его файлом")
        if response.headers.get("content-type", "").lower().startswith("text/"):
            raise Invalid("По ссылке страница, а не изображение. Откройте её и скопируйте прямую ссылку на файл "
                          "или скачайте изображение и загрузите его файлом")
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > policy.IMAGE_MAX_BYTES:
                raise Invalid("Изображение больше 10 МБ. Попросите покупателя прислать файл меньшего размера")
            if time.monotonic() > deadline:
                raise Invalid("Сайт отдаёт файл слишком долго. Скачайте изображение и загрузите его файлом")
    return bytes(data), None


def download_public(url, *, transport=None):
    """Fetch an image by a link a buyer sent. Run it outside a DB transaction."""
    target = _public_target(url)
    deadline = time.monotonic() + policy.LINK_DEADLINE_SECONDS
    timeout = httpx.Timeout(10, read=20)
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport) as client:
        for _ in range(policy.LINK_MAX_REDIRECTS + 1):
            content = location = None
            for address in _public_addresses(target):
                try:
                    content, location = _read_public(client, target, address, deadline)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    continue
                except httpx.HTTPError:
                    raise Invalid("Не удалось скачать файл с сайта. Скачайте изображение и загрузите его файлом") from None
            if content is not None:
                validate_image(content, {})
                return content
            if location is None:
                raise Invalid("Не удалось связаться с сайтом из ссылки. Проверьте адрес или загрузите изображение файлом")
            try:
                target = _public_target(target.join(location))
            except httpx.InvalidURL:
                raise Invalid("Сайт вернул некорректное перенаправление. Скачайте изображение и загрузите его файлом") from None
    raise Invalid("Слишком много перенаправлений. Скачайте изображение и загрузите его файлом")
