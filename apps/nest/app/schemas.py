from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roll_length_m: float = Field(default=50.0, gt=0, le=10_000)
    target_min_m: float = Field(default=47.0, gt=0, le=10_000)
    upper_tolerance_m: float = Field(default=1.5, ge=0, le=10_000)
    lower_soft_margin_m: float = Field(default=0.0, ge=0, le=10_000)

    job_gap_cm: float = Field(default=15.0, ge=0, le=100_000)
    leader_cm: float = Field(default=50.0, ge=0, le=100_000)
    trailer_cm: float = Field(default=50.0, ge=0, le=100_000)
    panel_gap_cm: float = Field(default=0.0, ge=0, le=100_000)
    top_bottom_margin_cm: float = Field(default=0.0, ge=0, le=100_000)
    default_height_cm: int = Field(default=270, gt=0, le=100_000)

    use_qty: bool = True
    brute_force_limit: int = Field(default=26, gt=0, le=10_000)
    partial_max_r: int = Field(default=10, gt=0, le=10_000)

    length_mode: Literal["per_panel", "combined"] = "per_panel"
    include_leader_trailer_in_target: bool = False

    completion_objective: Literal["max_groups", "min_additions", "exact_groups"] = (
        "max_groups"
    )
    completion_exact_groups: int = Field(default=0, ge=0, le=10_000)
    completion_allowed_add_widths: tuple[int, ...] = (300, 200, 100, 500)

    @field_validator("completion_allowed_add_widths")
    @classmethod
    def allowed_widths_must_be_positive(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("completion_allowed_add_widths cannot be empty.")
        if len(value) > 100 or any(width <= 0 for width in value):
            raise ValueError("completion_allowed_add_widths must contain positive widths.")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "SettingsIn":
        if self.target_min_m > self.roll_length_m:
            raise ValueError("target_min_m cannot be greater than roll_length_m.")
        if self.lower_soft_margin_m >= self.target_min_m:
            raise ValueError("lower_soft_margin_m must be less than target_min_m.")
        if self.include_leader_trailer_in_target:
            overhead_m = (self.leader_cm + self.trailer_cm) / 100
            if overhead_m >= self.roll_length_m:
                raise ValueError("leader_cm + trailer_cm must be shorter than the roll.")
        return self


class ImageIn(BaseModel):
    # Laravel may attach harmless identifiers or metadata that the calculator
    # does not need, so image-level extras are intentionally accepted.
    model_config = ConfigDict(extra="allow")

    article: str | None = Field(default=None, max_length=500)
    sku: str | None = Field(default=None, max_length=500)
    name: str | None = Field(default=None, max_length=500)
    id: str | int | None = None
    size: str | None = Field(
        default=None,
        max_length=100,
        description="Optional size string like 400x270.",
    )
    width_cm: int | None = Field(default=None, gt=0, le=100_000)
    height_cm: int | None = Field(default=None, gt=0, le=100_000)
    running_length_cm: float | None = Field(
        default=None,
        gt=0,
        le=1_000_000,
        description="Optional pre-calculated roll length before the configured job gap.",
    )
    calculation_group: str | None = Field(
        default=None,
        max_length=160,
        description="Optional optimization pool shared by items with different heights.",
    )
    qty: int | None = Field(default=None, ge=0, le=10_000_000)
    quantity: int | None = Field(default=None, ge=0, le=10_000_000)

    @model_validator(mode="after")
    def size_source_is_required(self) -> "ImageIn":
        if (
            self.width_cm is None
            and not self.size
            and not self.article
            and not self.name
            and not self.sku
        ):
            raise ValueError(
                "Provide width_cm or a size/article/name/sku containing a size like 400x270."
            )
        if self.qty is not None and self.quantity is not None and self.qty != self.quantity:
            raise ValueError("qty and quantity contain conflicting values.")
        return self


class CalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[ImageIn] = Field(default_factory=list, max_length=10_000)
    items: list[ImageIn] | None = Field(
        default=None,
        max_length=10_000,
        description="Alias for images; send either items or images, not both.",
    )
    settings: SettingsIn = Field(default_factory=SettingsIn)

    @model_validator(mode="after")
    def normalize_images(self) -> "CalculateRequest":
        if self.images and self.items:
            raise ValueError("Send either images or items, not both.")
        if not self.images and self.items:
            self.images = self.items
        if not self.images:
            raise ValueError("Request must contain at least one image.")
        return self
