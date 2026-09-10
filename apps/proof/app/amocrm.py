from __future__ import annotations

from typing import Any, BinaryIO, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .yandex_disk import YANDEX_DEALS_ROOT

MappingTarget = Literal["source_path", "layout_number", "order_number", "public_id"]
DeliveryMode = Literal["amocrm_attachment", "yandex_disk_note", "webhook"]
AMOCRM_HOST_SUFFIXES = (".amocrm.ru", ".amocrm.com", ".kommo.com")


class AmoFieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: MappingTarget
    field_id: int = Field(gt=0)


class AmoIntegrationConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")
    api_base_url: str = ""
    api_timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    queued_status_id: int | None = Field(default=None, gt=0)
    completed_status_id: int | None = Field(default=None, gt=0)
    failed_status_id: int | None = Field(default=None, gt=0)
    delivery_mode: DeliveryMode = "amocrm_attachment"
    mappings: list[AmoFieldMapping] = Field(default_factory=list, max_length=3)
    clear_field_ids: list[int] = Field(default_factory=list, max_length=3)
    fields_cache: list[dict[str, Any]] = Field(default_factory=list)
    statuses_cache: list[dict[str, Any]] = Field(default_factory=list)
    account_id: int | None = Field(default=None, gt=0)
    account_name: str = Field(default="", max_length=255)
    connected_at: str = Field(default="", max_length=64)
    yandex_disk_root: str = Field(default=YANDEX_DEALS_ROOT, max_length=1000)

    @field_validator("api_base_url")
    @classmethod
    def safe_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or not hostname.endswith(AMOCRM_HOST_SUFFIXES)
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Укажите HTTPS-адрес аккаунта amoCRM или Kommo без пути.")
        return value

    @field_validator("yandex_disk_root")
    @classmethod
    def fixed_yandex_disk_root(cls, _value: str) -> str:
        return YANDEX_DEALS_ROOT

    @model_validator(mode="after")
    def unique_targets(self):
        targets = [mapping.target for mapping in self.mappings]
        if len(targets) != len(set(targets)):
            raise ValueError("Каждое назначение данных можно настроить только один раз.")
        field_ids = [mapping.field_id for mapping in self.mappings]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("Одно поле amoCRM нельзя использовать в нескольких назначениях.")
        if len(self.clear_field_ids) != len(set(self.clear_field_ids)):
            raise ValueError("Поля для очистки не должны повторяться.")
        if any(field_id not in field_ids for field_id in self.clear_field_ids):
            raise ValueError("Очищать можно только поля из трёх выбранных привязок.")
        return self

    def mapping_for(self, target: MappingTarget) -> int | None:
        return next(
            (mapping.field_id for mapping in self.mappings if mapping.target == target),
            None,
        )

    def has_required_mappings(self) -> bool:
        return len(self.mappings) == 3 and {
            "source_path",
            "layout_number",
        }.issubset({mapping.target for mapping in self.mappings})


class AmoOAuthTokenSet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token_type: str
    expires_in: int = Field(gt=0)
    server_time: int = Field(gt=0)
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)

    @property
    def expires_at(self) -> int:
        return self.server_time + self.expires_in


def amocrm_authorization_url(client_id: str, api_base_url: str, state: str) -> str:
    host = urlsplit(api_base_url).hostname or ""
    oauth_host = "www.amocrm.ru" if host.endswith(".amocrm.ru") else "www.amocrm.com"
    return f"https://{oauth_host}/oauth?{urlencode({
        'client_id': client_id,
        'state': state,
        'mode': 'popup',
    })}"


def normalize_amocrm_referer(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    return AmoIntegrationConfiguration(api_base_url=normalized).api_base_url


def exchange_amocrm_oauth_token(
    api_base_url: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    grant_type: Literal["authorization_code", "refresh_token"],
    code: str = "",
    refresh_token: str = "",
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> AmoOAuthTokenSet:
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": grant_type,
        "redirect_uri": redirect_uri,
    }
    if grant_type == "authorization_code":
        payload["code"] = code
    else:
        payload["refresh_token"] = refresh_token
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
    try:
        response = http_client.post(f"{api_base_url}/oauth2/access_token", json=payload)
    finally:
        if owns_client:
            http_client.close()
    if response.status_code != 200:
        raise RuntimeError(f"amoCRM OAuth returned HTTP {response.status_code}.")
    try:
        return AmoOAuthTokenSet.model_validate(response.json())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("amoCRM OAuth returned an invalid token response.") from exc


def parse_optional_id(value: str) -> int | None:
    normalized = value.strip()
    if not normalized:
        return None
    if not normalized.isdecimal() or int(normalized) < 1:
        raise ValueError("ID amoCRM должен быть положительным целым числом.")
    return int(normalized)


def custom_field_value(lead: dict[str, Any], field_id: int) -> Any:
    for field in lead.get("custom_fields_values") or []:
        if not isinstance(field, dict) or field.get("field_id") != field_id:
            continue
        values = field.get("values") or []
        if not values or not isinstance(values[0], dict):
            return None
        return values[0].get("value")
    return None


def build_job_input(lead: dict[str, Any], configuration: AmoIntegrationConfiguration) -> dict[str, Any]:
    result: dict[str, Any] = {"metadata": {"amo_lead_id": lead.get("id")}}
    for mapping in configuration.mappings:
        value = custom_field_value(lead, mapping.field_id)
        if mapping.target == "layout_number":
            if isinstance(value, bool):
                raise ValueError("Поле номера макета должно содержать целое число.")
            try:
                value = int(str(value).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError("Поле номера макета должно содержать целое число.") from exc
        elif value is not None:
            value = str(value).strip()
        result[mapping.target] = value
    if not isinstance(result.get("source_path"), str) or not result["source_path"]:
        raise ValueError("В сделке не заполнено настроенное поле пути заказа.")
    if not isinstance(result.get("layout_number"), int) or not 1 <= result["layout_number"] <= 999999:
        raise ValueError("В сделке отсутствует корректный номер макета.")
    return result


class AmoClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout_seconds: float | None,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url or not access_token:
            raise ValueError("Для чтения сделки нужны URL аккаунта и Access token amoCRM.")
        if timeout_seconds is None or timeout_seconds <= 0:
            raise ValueError("Укажите HTTP timeout amoCRM в настройках интеграции.")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self.headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(
            method, f"{self.base_url}{path}", headers=self.headers, **kwargs
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"amoCRM API returned HTTP {response.status_code}.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("amoCRM API returned an invalid response.")
        return payload

    @staticmethod
    def _safe_drive_url(value: Any) -> str:
        if not isinstance(value, str):
            raise RuntimeError("amoCRM account response has no file service URL.")
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or not hostname
            or not hostname.endswith(AMOCRM_HOST_SUFFIXES)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("amoCRM returned an unsafe file service URL.")
        return normalized

    @staticmethod
    def _validate_upload_url(value: Any, drive_url: str) -> str:
        if not isinstance(value, str):
            raise RuntimeError("amoCRM file upload response has no upload URL.")
        upload = urlsplit(value)
        drive = urlsplit(drive_url)
        if (
            upload.scheme != drive.scheme
            or upload.hostname != drive.hostname
            or upload.port != drive.port
            or upload.username
            or upload.password
            or upload.fragment
        ):
            raise RuntimeError("amoCRM returned an unsafe file upload URL.")
        return value

    def _absolute_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, url, headers=headers, **kwargs)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"amoCRM file service returned HTTP {response.status_code}.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("amoCRM file service returned an invalid response.")
        return payload

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        if not lead_id.isdecimal():
            raise ValueError("Webhook amoCRM не содержит корректный ID сделки.")
        return self._request("GET", f"/api/v4/leads/{lead_id}")

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/api/v4/account")

    def get_drive_url(self) -> str:
        account = self._request("GET", "/api/v4/account?with=drive_url")
        return self._safe_drive_url(account.get("drive_url"))

    def upload_file(
        self,
        file_obj: BinaryIO,
        *,
        file_name: str,
        file_size: int,
        content_type: str,
    ) -> dict[str, Any]:
        drive_url = self.get_drive_url()
        session_payload = self._absolute_request(
            "POST",
            f"{drive_url}/v1.0/sessions",
            json={
                "file_name": file_name,
                "file_size": file_size,
                "content_type": content_type,
            },
        )
        upload_url = self._validate_upload_url(session_payload.get("upload_url"), drive_url)
        try:
            max_part_size = int(session_payload.get("max_part_size"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("amoCRM returned an invalid upload chunk size.") from exc
        if max_part_size < 1:
            raise RuntimeError("amoCRM returned an invalid upload chunk size.")

        uploaded = 0
        final_payload: dict[str, Any] | None = None
        while uploaded < file_size:
            chunk = file_obj.read(min(max_part_size, file_size - uploaded))
            if not chunk:
                raise RuntimeError("Result file ended before the declared file size.")
            final_payload = self._absolute_request(
                "POST",
                upload_url,
                content=chunk,
                headers={"Content-Type": content_type},
            )
            uploaded += len(chunk)
            next_url = final_payload.get("next_url")
            if next_url:
                upload_url = self._validate_upload_url(next_url, drive_url)
            elif uploaded != file_size:
                raise RuntimeError("amoCRM ended the upload before receiving the whole file.")

        if final_payload is None:
            raise RuntimeError("An empty file cannot be uploaded to amoCRM.")
        file_uuid = final_payload.get("uuid")
        version_uuid = final_payload.get("version_uuid")
        if not isinstance(file_uuid, str) or not file_uuid:
            raise RuntimeError("amoCRM did not return the uploaded file UUID.")
        if not isinstance(version_uuid, str) or not version_uuid:
            raise RuntimeError("amoCRM did not return the uploaded file version UUID.")
        return final_payload

    def is_file_linked(self, lead_id: str, file_uuid: str) -> bool:
        if not lead_id.isdecimal():
            raise ValueError("Job does not contain a valid amoCRM lead ID.")
        before_id: int | None = None
        for _page in range(100):
            path = f"/api/v4/leads/{lead_id}/files?limit=250"
            if before_id is not None:
                path += f"&before_id={before_id}"
            response = self.client.request(
                "GET", f"{self.base_url}{path}", headers=self.headers
            )
            if response.status_code == 204:
                return False
            if not 200 <= response.status_code < 300:
                raise RuntimeError(
                    f"amoCRM API returned HTTP {response.status_code}."
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("amoCRM API returned an invalid response.")
            files = payload.get("_embedded", {}).get("files", [])
            if not isinstance(files, list):
                raise RuntimeError("amoCRM returned an invalid linked files response.")
            if any(
                isinstance(item, dict) and item.get("file_uuid") == file_uuid
                for item in files
            ):
                return True
            if len(files) < 250:
                return False
            relation_ids = [
                item.get("id")
                for item in files
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
            if not relation_ids:
                raise RuntimeError("amoCRM linked files page has no relation IDs.")
            before_id = min(relation_ids)
        raise RuntimeError("amoCRM linked files lookup exceeded the safe page limit.")

    def link_file(self, lead_id: str, file_uuid: str) -> None:
        if not lead_id.isdecimal():
            raise ValueError("Job does not contain a valid amoCRM lead ID.")
        response = self.client.request(
            "PUT",
            f"{self.base_url}/api/v4/leads/{lead_id}/files",
            headers=self.headers,
            json=[{"file_uuid": file_uuid}],
        )
        if response.status_code != 202:
            raise RuntimeError(f"amoCRM API returned HTTP {response.status_code}.")

    def find_common_note(self, lead_id: str, text: str) -> dict[str, Any] | None:
        if not lead_id.isdecimal():
            raise ValueError("Job does not contain a valid amoCRM lead ID.")
        for page in range(1, 101):
            response = self.client.request(
                "GET",
                f"{self.base_url}/api/v4/leads/{lead_id}/notes"
                f"?filter[note_type]=common&limit=250&page={page}",
                headers=self.headers,
            )
            if response.status_code == 204:
                return None
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"amoCRM API returned HTTP {response.status_code}.")
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("amoCRM returned an invalid notes response.")
            notes = payload.get("_embedded", {}).get("notes", [])
            if not isinstance(notes, list):
                raise RuntimeError("amoCRM returned an invalid notes response.")
            for note in notes:
                if (
                    isinstance(note, dict)
                    and isinstance(note.get("params"), dict)
                    and note["params"].get("text") == text
                ):
                    return note
            if len(notes) < 250:
                return None
        raise RuntimeError("amoCRM note lookup exceeded the safe page limit.")

    def create_common_note(self, lead_id: str, text: str) -> dict[str, Any]:
        if not lead_id.isdecimal():
            raise ValueError("Job does not contain a valid amoCRM lead ID.")
        return self._request(
            "POST",
            f"/api/v4/leads/{lead_id}/notes",
            json={"note_type": "common", "params": {"text": text}},
        )

    def load_catalog(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        def load_pages(path: str, collection_name: str) -> list[dict[str, Any]]:
            collected: list[dict[str, Any]] = []
            for page in range(1, 101):
                separator = "&" if "?" in path else "?"
                payload = self._request("GET", f"{path}{separator}limit=250&page={page}")
                items = payload.get("_embedded", {}).get(collection_name, [])
                if not isinstance(items, list):
                    raise RuntimeError("amoCRM returned an invalid catalog response.")
                collected.extend(item for item in items if isinstance(item, dict))
                if not payload.get("_links", {}).get("next"):
                    return collected
            raise RuntimeError("amoCRM catalog exceeded the safe page limit.")

        field_items = load_pages("/api/v4/leads/custom_fields", "custom_fields")
        pipeline_items = load_pages("/api/v4/leads/pipelines", "pipelines")
        fields = [
            {"id": item.get("id"), "name": item.get("name", ""), "type": item.get("type", "")}
            for item in field_items
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]
        statuses = []
        for pipeline in pipeline_items:
            if not isinstance(pipeline, dict):
                continue
            for status in pipeline.get("_embedded", {}).get("statuses", []):
                if isinstance(status, dict) and isinstance(status.get("id"), int):
                    statuses.append({
                        "id": status["id"],
                        "name": status.get("name", ""),
                        "pipeline_id": pipeline.get("id"),
                        "pipeline_name": pipeline.get("name", ""),
                    })
        return fields, statuses

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/v4/leads/{lead_id}", json=payload)
