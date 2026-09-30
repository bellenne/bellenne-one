"""Server-side Mattermost Incoming Webhook adapter for scenario actions."""

import ipaddress
from urllib.parse import urlsplit

import httpx

from . import security


class MattermostError(RuntimeError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.code = code
        self.unknown = unknown


def normalize_webhook_url(value):
    url = value.strip()
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if (parsed.scheme != "https" or not hostname or hostname == "localhost"
            or hostname.endswith(".localhost") or (address is not None and not address.is_global)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or "/hooks/" not in parsed.path or parsed.path.endswith("/hooks/")):
        raise ValueError("Укажите HTTPS-адрес входящего webhook Mattermost вида /hooks/…")
    return url


def integration(con):
    return con.execute("SELECT * FROM mattermost_integrations WHERE id=1").fetchone()


def webhook_url(record):
    if not record:
        raise MattermostError("mattermost_not_configured")
    try:
        return normalize_webhook_url(security.cipher().decrypt(record["secret"].encode()).decode())
    except Exception as exc:
        raise MattermostError("mattermost_credentials_unavailable") from exc


def send(record, message, *, transport=None):
    url = webhook_url(record)
    try:
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport) as client:
            response = client.post(url, json={"text": message})
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise MattermostError("mattermost_transport_unknown", unknown=True) from exc
    if 200 <= response.status_code < 300:
        return
    raise MattermostError(
        f"mattermost_http_{response.status_code}",
        unknown=response.status_code >= 500,
    )
