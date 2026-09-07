from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .core import Settings
from .models import ApiToken, UserConfiguration, utc_now
from .schemas import SettingsIn
from .security import generate_api_token, token_digest, token_parts


def default_settings() -> SettingsIn:
    return SettingsIn(**asdict(Settings()))


def load_configuration(
    session: Session,
    external_user_id: int | None,
    username: str,
) -> SettingsIn:
    query = select(UserConfiguration)
    if external_user_id is not None:
        query = query.where(UserConfiguration.external_user_id == external_user_id)
    elif username:
        query = query.where(UserConfiguration.username == username)
    else:
        return default_settings()
    configuration = session.scalar(query)
    if configuration is None:
        return default_settings()
    return SettingsIn.model_validate_json(configuration.configuration_json)


def save_configuration(
    session: Session,
    external_user_id: int,
    username: str,
    settings: SettingsIn,
) -> UserConfiguration:
    configuration = session.scalar(
        select(UserConfiguration).where(
            or_(
                UserConfiguration.external_user_id == external_user_id,
                UserConfiguration.username == username,
            )
        )
    )
    if configuration is None:
        configuration = UserConfiguration(
            external_user_id=external_user_id,
            username=username,
            configuration_json="{}",
        )
        session.add(configuration)
    configuration.external_user_id = external_user_id
    configuration.username = username
    configuration.configuration_json = json.dumps(
        settings.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    session.flush()
    return configuration


def find_api_token_for_user(session: Session, external_user_id: int) -> ApiToken | None:
    return session.scalar(
        select(ApiToken).where(ApiToken.external_user_id == external_user_id)
    )


def authenticate_api_token(session: Session, raw_token: str) -> ApiToken | None:
    if not raw_token:
        return None
    return session.scalar(
        select(ApiToken).where(ApiToken.token_digest == token_digest(raw_token))
    )


def rotate_api_token(
    session: Session,
    external_user_id: int,
    owner_username: str,
) -> tuple[ApiToken, str]:
    raw_token = generate_api_token()
    prefix, last_four = token_parts(raw_token)
    credential = find_api_token_for_user(session, external_user_id)
    if credential is None:
        credential = ApiToken(
            external_user_id=external_user_id,
            owner_username=owner_username,
            token_digest="",
            token_prefix=prefix,
            token_last_four=last_four,
        )
        session.add(credential)
    credential.owner_username = owner_username
    credential.token_digest = token_digest(raw_token)
    credential.token_prefix = prefix
    credential.token_last_four = last_four
    generated_at = utc_now()
    credential.created_at = generated_at
    credential.updated_at = generated_at
    credential.last_used_at = None
    session.flush()
    return credential, raw_token


def summarize_calculation_result(response: dict[str, Any]) -> dict[str, int | float]:
    efficient_groups = response.get("impositions") or []
    remainder_groups = response.get("leftover_impositions") or []

    def group_rolls(groups: list[dict[str, Any]]) -> int:
        return sum(max(0, int(group.get("repeat") or 0)) for group in groups)

    def group_material(groups: list[dict[str, Any]]) -> float:
        return sum(
            max(0.0, float(group.get("total_m") or 0))
            * max(0, int(group.get("repeat") or 0))
            for group in groups
        )

    efficient_rolls = group_rolls(efficient_groups)
    remainder_rolls = group_rolls(remainder_groups)
    rolls_used = efficient_rolls + remainder_rolls
    material_used_m = group_material(efficient_groups) + group_material(remainder_groups)
    roll_length_m = max(
        0.0,
        float((response.get("settings_used") or {}).get("roll_length_m") or 0),
    )
    material_remaining_m = max(0.0, roll_length_m * rolls_used - material_used_m)
    result_summary = response.get("summary") or {}
    inefficient_units = max(0, int(result_summary.get("inefficient_units") or 0))
    unplaced_units = max(0, int(result_summary.get("unplaced_units") or 0))

    return {
        "rolls_used": rolls_used,
        "efficient_rolls": efficient_rolls,
        "remainder_rolls": remainder_rolls,
        "material_used_m": round(material_used_m, 2),
        "material_remaining_m": round(material_remaining_m, 2),
        "remaining_units": inefficient_units + unplaced_units,
        "unplaced_units": unplaced_units,
    }
