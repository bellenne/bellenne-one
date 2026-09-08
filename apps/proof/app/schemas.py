from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


WorkerAvailability = Literal["available", "busy", "error"]


class HeartbeatRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=80)
    availability: WorkerAvailability
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    current_job_id: str | None = None
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=2000)


class ProgressRequest(BaseModel):
    progress: int = Field(ge=0, le=100)
    current_stage: str = Field(default="", max_length=160)


class WorkerEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=120)
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    message: str = Field(min_length=1, max_length=4000)
    error_code: str | None = Field(default=None, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)


class FailureRequest(BaseModel):
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)


class WebhookRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=120)
    crm_entity_type: str = Field(min_length=1, max_length=80)
    crm_entity_id: str = Field(min_length=1, max_length=120)
    crm_order_id: str = Field(default="", max_length=120)
    preset_id: int | None = None
    input: dict[str, Any]

    @field_validator("crm_entity_type")
    @classmethod
    def supported_entity(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"leads", "customers"}:
            raise ValueError("crm_entity_type must be leads or customers")
        return normalized
