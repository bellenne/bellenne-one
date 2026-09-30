"""Minimal RetailCRM v5 adapter used by the scenario integration outbox."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from urllib.parse import quote, urlsplit

import httpx

from . import db, security


class RetailCRMError(RuntimeError):
    def __init__(self, code: str, *, unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.unknown = unknown


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").casefold()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or (address is not None and not address.is_global)
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Укажите публичный HTTPS-адрес RetailCRM без пути.")
    return normalized


def integration(con):
    return con.execute("SELECT * FROM retailcrm_integrations WHERE id=1").fetchone()


def external_id(instance_id: int) -> str:
    return f"folio_instance_{instance_id}"


def public_media_base_url() -> str:
    value = os.environ.get("FOLIO_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise RetailCRMError("retailcrm_public_base_url_missing")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise RetailCRMError("retailcrm_public_base_url_invalid") from exc
    hostname = (parsed.hostname or "").casefold()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RetailCRMError("retailcrm_public_base_url_invalid")
    return value


def photo_links(con, row, photo_ids: list[str]) -> list[str]:
    if not photo_ids:
        return []
    base_url = public_media_base_url()
    prefix = os.environ.get("MODULE_PREFIX", "/folio").rstrip("/")
    links = []
    for mid in photo_ids:
        if not isinstance(mid, str) or not re.fullmatch(r"[0-9a-f]{32}", mid):
            raise RetailCRMError("retailcrm_media_missing")
        attachment = con.execute(
            "SELECT id FROM media WHERE id=? AND chat_id=? AND item_id=?",
            (mid, row["chat_id"], row["item_id"]),
        ).fetchone()
        if not attachment or not (db.DATA / "media" / mid).is_file():
            raise RetailCRMError("retailcrm_media_missing")
        links.append(f"{base_url}{prefix}/media/{quote(mid, safe='')}")
    return links


def api_key(record) -> str:
    if not record or not record["secret"]:
        raise RetailCRMError("retailcrm_not_configured")
    try:
        return security.cipher().decrypt(record["secret"].encode()).decode()
    except Exception as exc:  # noqa: BLE001 -- never expose encrypted values
        raise RetailCRMError("retailcrm_credentials_unavailable") from exc


class RetailCRMAdapter:
    def __init__(self, record, *, transport=None):
        self.base_url = normalize_base_url(record["base_url"])
        self.site = str(record["site"]).strip()
        if not self.site:
            raise RetailCRMError("retailcrm_not_configured")
        self._api_key = api_key(record)
        self.client = httpx.Client(
            timeout=float(
                os.environ.get("FOLIO_RETAILCRM_HTTP_TIMEOUT_SECONDS") or "20"
            ),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self):
        self.client.close()

    def _request(self, method: str, path: str, **kwargs):
        mutating = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        parameter = "data" if mutating else "params"
        authorization = dict(kwargs.pop(parameter, {}) or {})
        authorization["apiKey"] = self._api_key
        kwargs[parameter] = authorization
        try:
            response = self.client.request(method, self.base_url + path, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetailCRMError(
                "retailcrm_transport_unknown" if mutating else "retailcrm_transport_failed",
                unknown=mutating,
            ) from exc
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise RetailCRMError(
                f"retailcrm_http_{response.status_code}",
                unknown=mutating and response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RetailCRMError(
                "retailcrm_invalid_response", unknown=mutating
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise RetailCRMError("retailcrm_invalid_response", unknown=mutating)
        return payload

    def credentials(self):
        payload = self._request("GET", "/api/credentials")
        if not isinstance(payload, dict):
            raise RetailCRMError("retailcrm_invalid_response")
        scopes = payload.get("scopes")
        sites = payload.get("sitesAvailable")
        return {
            "order_read": isinstance(scopes, list) and "order_read" in scopes,
            "order_write": isinstance(scopes, list) and "order_write" in scopes,
            "reference_read": isinstance(scopes, list) and "reference_read" in scopes,
            "site": payload.get("siteAccess") == "access_full"
            or (isinstance(sites, list) and self.site in sites),
        }

    def statuses(self):
        payload = self._request("GET", "/api/v5/reference/statuses")
        statuses = payload.get("statuses") if isinstance(payload, dict) else None
        # RetailCRM may return a code-keyed object even though its API
        # reference describes statuses as an array.
        if isinstance(statuses, dict):
            statuses = list(statuses.values())
        if not isinstance(statuses, list):
            raise RetailCRMError("retailcrm_statuses_unavailable")
        return [
            {"code": row["code"], "name": row["name"], "group": row.get("group", "")}
            for row in statuses
            if isinstance(row, dict) and row.get("active") is True
            and isinstance(row.get("code"), str) and isinstance(row.get("name"), str)
        ]

    def find_order(self, external_id: str):
        return self._request(
            "GET",
            f"/api/v5/orders/{quote(external_id, safe='')}",
            params={"by": "externalId", "site": self.site},
        )

    def create_order(self, order: dict):
        payload = self._request(
            "POST",
            "/api/v5/orders/create",
            data={
                "site": self.site,
                "order": json.dumps(order, ensure_ascii=False, separators=(",", ":")),
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise RetailCRMError("retailcrm_invalid_response", unknown=True)
        return payload

    def edit_order_customer_comment(self, external_id: str, comment: str,
                                    items: list[dict], status: str = ""):
        order = {"customerComment": comment}
        if status:
            order["status"] = status
        if items:
            if not all(isinstance(item.get("id"), int) for item in items):
                raise RetailCRMError("retailcrm_items_unverified")
            # RetailCRM removes omitted order items on edit. Include every
            # existing item by ID, without changing its fields.
            order["items"] = [{"id": item["id"]} for item in items]
        payload = self._request(
            "POST", f"/api/v5/orders/{quote(external_id, safe='')}/edit",
            data={"by": "externalId", "site": self.site,
                  "order": json.dumps(order, ensure_ascii=False, separators=(",", ":"))},
        )
        if not isinstance(payload, dict):
            raise RetailCRMError("retailcrm_invalid_response", unknown=True)
        return payload


def order_payload(con, instance_id: int, node: dict) -> tuple[str, dict]:
    row = con.execute(
        "SELECT i.id AS instance_id,i.chat_id,i.item_id,i.fields,"
        "t.posting,t.sku,t.offer_id,t.name,t.quantity "
        "FROM instances i JOIN items t ON t.id=i.item_id WHERE i.id=?",
        (instance_id,),
    ).fetchone()
    if not row:
        raise RetailCRMError("retailcrm_instance_missing")
    fields = json.loads(row["fields"])
    comment_template = str(node.get("comment") or "")
    includes_photos = "{summary}" in comment_template or "{photos}" in comment_template
    labels = {
        "photos": "Фото",
        "background": "Фон",
        "caption": "Надпись",
        "wishes": "Согласование",
        "template_id": "Шаблон",
    }
    visible = {}
    for key, value in fields.items():
        if key == "photos":
            if not includes_photos:
                continue
            if not isinstance(value, list):
                raise RetailCRMError("retailcrm_media_missing")
            links = photo_links(con, row, value)
            if links:
                visible[key] = (
                    links[0]
                    if len(links) == 1
                    else "\n".join(
                        f"{number}. {link}"
                        for number, link in enumerate(links, start=1)
                    )
                )
        else:
            visible[key] = str(value)
    values = {
        **visible,
        "posting": row["posting"],
        "sku": row["offer_id"],
        "offer_id": row["offer_id"],
        "product_name": row["name"],
        "quantity": str(row["quantity"]),
        "summary": "\n".join(
            f"{labels.get(key, key)}: {value}" for key, value in visible.items()
        ),
    }
    try:
        comment = comment_template.format_map(values).strip()
    except (KeyError, ValueError) as exc:
        raise RetailCRMError("retailcrm_template_invalid") from exc
    order_id = external_id(row["instance_id"])
    order = {"externalId": order_id, "managerComment": comment}
    for source, target in (
        ("status", "status"),
        ("order_type", "orderType"),
        ("order_method", "orderMethod"),
    ):
        value = str(node.get(source) or "").strip()
        if value:
            order[target] = value
    return order_id, order
