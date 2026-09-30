"""Seller API boundary. No endpoint fallback and no implicit retry of writes.

The account probe establishes read capabilities. Starting chats and sending text
require the administrator's explicit activation; successful writes are recorded
as evidence, never inferred from a successful list request.
"""

import base64
import os

import httpx

from .security import cipher


SELLER_INFO_PATH = "/v1/seller/info"
CHAT_LIST_PATH = "/v3/chat/list"
CHAT_HISTORY_PATH = "/v3/chat/history"
CHAT_SEND_MESSAGE_PATH = "/v1/chat/send/message"
CHAT_SEND_FILE_PATH = "/v1/chat/send/file"
CHAT_START_PATH = "/v1/chat/start"
FBS_POSTING_LIST_PATH = "/v4/posting/fbs/list"
PRODUCT_LIST_PATH = "/v3/product/list"


class OzonError(Exception):
    def __init__(self, code, unknown=False, retry_after=None):
        self.code, self.unknown = code, unknown
        self.retry_after = retry_after
        super().__init__(code)


class OzonAdapter:
    def __init__(self, account, settings=None, transport=None):
        client_options = {}
        configured_timeout = os.environ.get("FOLIO_OZON_HTTP_TIMEOUT_SECONDS", "").strip()
        self.write_timeout = None
        if configured_timeout:
            try:
                timeout = float(configured_timeout)
            except ValueError as exc:
                raise RuntimeError("FOLIO_OZON_HTTP_TIMEOUT_SECONDS must be a positive number") from exc
            if timeout <= 0:
                raise RuntimeError("FOLIO_OZON_HTTP_TIMEOUT_SECONDS must be a positive number")
            client_options["timeout"] = timeout
        else:
            # Ozon can take longer than httpx's five-second default to confirm
            # a write. Keep the short connection limit and never retry a write.
            self.write_timeout = httpx.Timeout(30.0, connect=5.0)
        self.client = httpx.Client(
            base_url="https://api-seller.ozon.ru",
            headers={
                "Client-Id": account["client_id"],
                "Api-Key": cipher().decrypt(account["secret"].encode()).decode(),
            },
            follow_redirects=False,
            transport=transport,
            **client_options,
        )

    def close(self):
        self.client.close()

    def post(self, path, body, write=False):
        try:
            options = {"timeout": self.write_timeout} if write and self.write_timeout else {}
            response = self.client.post(path, json=body, **options)
        except httpx.ConnectError:
            raise OzonError("connection_failed") from None
        except httpx.ConnectTimeout:
            raise OzonError(
                "transport_unknown" if write else "connection_failed",
                unknown=write,
            ) from None
        except httpx.ReadTimeout:
            raise OzonError(
                "read_timeout_unknown" if write else "transport_failed",
                unknown=write,
            ) from None
        except httpx.WriteTimeout:
            raise OzonError(
                "write_timeout_unknown" if write else "transport_failed",
                unknown=write,
            ) from None
        except httpx.RequestError:
            raise OzonError(
                "transport_unknown" if write else "transport_failed", unknown=write
            ) from None
        if response.status_code >= 400:
            retry_after = response.headers.get("Retry-After", "")
            retry_after = int(retry_after) if retry_after.isdigit() else None
            raise OzonError(
                f"ozon_http_{response.status_code}",
                unknown=write and response.status_code >= 500,
                retry_after=retry_after,
            )
        if response.status_code != 200:
            raise OzonError("unexpected_http_status", unknown=write)
        try:
            value = response.json()
        except ValueError:
            raise OzonError("invalid_response", unknown=write) from None
        if not isinstance(value, dict):
            raise OzonError("invalid_response", unknown=write)
        return value

    def chat_page(self, limit=100):
        result = self.post(
            CHAT_LIST_PATH,
            {"limit": limit, "cursor": "", "filter": {"unread_only": False}},
        )
        if not isinstance(result.get("chats"), list):
            raise OzonError("chat_list_contract_changed")
        return result

    def history_page(self, chat_id, limit=100):
        result = self.post(
            CHAT_HISTORY_PATH,
            {"chat_id": chat_id, "limit": limit, "direction": "Backward"},
        )
        if not isinstance(result.get("messages"), list):
            raise OzonError("chat_history_contract_changed")
        return result

    def probe_orders(self, since, until):
        result = self.post(
            FBS_POSTING_LIST_PATH,
            {
                "sort_dir": "ASC",
                "filter": {"since": since, "to": until},
                "limit": 1,
                "cursor": "",
            },
        )
        page = (
            result.get("result")
            if isinstance(result.get("result"), dict)
            else result
        )
        if not isinstance(page.get("postings"), list):
            raise OzonError("orders_contract_changed")
        return page

    def chats(self):
        cursor, seen = "", set()
        while True:
            result = self.post(CHAT_LIST_PATH, {"limit": 100, "cursor": cursor, "filter": {"unread_only": False}})
            if not isinstance(result.get("chats"), list):
                raise OzonError("chat_list_contract_changed")
            yield from result["chats"]
            if not result.get("has_next"):
                return
            cursor = result.get("cursor")
            if not cursor or cursor in seen:
                raise OzonError("chat_cursor_stalled")
            seen.add(cursor)

    def history(self, chat_id):
        cursor, seen = None, set()
        while True:
            payload = {"chat_id": chat_id, "limit": 100, "direction": "Backward"}
            if cursor:
                payload["from_message_id"] = cursor
            result = self.post(CHAT_HISTORY_PATH, payload)
            messages = result.get("messages")
            if not isinstance(messages, list):
                raise OzonError("chat_history_contract_changed")
            yield from messages
            if not result.get("has_next"):
                return
            cursor = str(messages[-1].get("message_id", "")) if messages else ""
            if not cursor or cursor in seen:
                raise OzonError("history_cursor_stalled")
            seen.add(cursor)

    def orders(self, since, until):
        for rows, _ in self.order_pages(since, until):
            yield from rows

    def products(self):
        """Read seller articles from the paginated product catalogue."""
        last_id, seen = "", set()
        while True:
            result = self.post(
                PRODUCT_LIST_PATH,
                {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 100},
            )
            page = result.get("result") if isinstance(result.get("result"), dict) else result
            rows = page.get("items")
            if not isinstance(rows, list):
                raise OzonError("product_list_contract_changed")
            yield from rows
            cursor = page.get("last_id")
            if not cursor:
                return
            if not rows or cursor in seen:
                raise OzonError("product_cursor_stalled")
            seen.add(cursor)
            last_id = cursor

    def order_pages(self, since, until, cursor="", order_numbers=None):
        # Numeric offsets belonged to the retired v3 contract. Restarting an
        # interrupted legacy page is safe because imports are idempotent.
        cursor = cursor if isinstance(cursor, str) else ""
        seen = set()
        while True:
            filters = {"since": since, "to": until}
            if order_numbers:
                filters["order_numbers"] = list(order_numbers)
            result = self.post(
                FBS_POSTING_LIST_PATH,
                {
                    "sort_dir": "ASC",
                    "filter": filters,
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            page = (
                result.get("result")
                if isinstance(result.get("result"), dict)
                else result
            )
            rows = page.get("postings")
            if not isinstance(rows, list):
                raise OzonError("orders_contract_changed")
            next_cursor = page.get("cursor") if page.get("has_next") else None
            yield rows, next_cursor
            if not page.get("has_next"):
                return
            if not rows or not next_cursor or next_cursor in seen:
                raise OzonError("orders_cursor_stalled")
            seen.add(next_cursor)
            cursor = next_cursor

    def send(self, chat_id, text):
        result = self.post(
            CHAT_SEND_MESSAGE_PATH, {"chat_id": chat_id, "text": text}, write=True
        )
        payload = result.get("result", result)
        # A string can be either a real message ID or merely an acknowledgement
        # such as "success". Never persist an acknowledgement as a message ID:
        # the actual seller message must be matched from chat history.
        message_id = payload.get("message_id") if isinstance(payload, dict) else payload
        if (isinstance(message_id, bool)
                or not isinstance(message_id, (str, int))
                or not message_id
                or str(message_id).casefold() in {"success", "ok"}):
            raise OzonError("send_result_unknown", unknown=True)
        return str(message_id)

    def send_file(self, chat_id, filename, content):
        result = self.post(
            CHAT_SEND_FILE_PATH,
            {"chat_id": chat_id, "name": filename,
             "base64_content": base64.b64encode(content).decode("ascii")},
            write=True,
        )
        payload = result.get("result", result)
        message_id = payload.get("message_id") if isinstance(payload, dict) else payload
        if (isinstance(message_id, bool) or not isinstance(message_id, (str, int))
                or not message_id or str(message_id).casefold() in {"success", "ok"}):
            raise OzonError("send_file_result_unknown", unknown=True)
        return str(message_id)

    def start(self, posting):
        result = self.post(CHAT_START_PATH, {"posting_number": posting}, write=True)
        chat_id = (result.get("result") or result).get("chat_id")
        if not chat_id:
            raise OzonError("start_result_unknown", unknown=True)
        return str(chat_id)

    def seller_info(self):
        result = self.post(SELLER_INFO_PATH, {})
        if not result:
            raise OzonError("seller_info_contract_changed")
        return result
