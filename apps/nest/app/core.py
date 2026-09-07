import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Any


SIZE_RE = re.compile(r"(\d{2,4})\s*[\*xXхХ×]\s*(\d{2,4})", re.I)
LENGTH_SCALE = 10  # tenths of a centimetre; keeps float settings deterministic


@dataclass(frozen=True)
class Settings:
    roll_length_m: float = 50.0
    target_min_m: float = 47.0
    upper_tolerance_m: float = 1.5
    lower_soft_margin_m: float = 0.0

    job_gap_cm: float = 15.0
    leader_cm: float = 50.0
    trailer_cm: float = 50.0
    panel_gap_cm: float = 0.0
    top_bottom_margin_cm: float = 0.0
    default_height_cm: int = 270

    use_qty: bool = True
    brute_force_limit: int = 26
    partial_max_r: int = 10

    length_mode: str = "per_panel"
    include_leader_trailer_in_target: bool = False

    completion_objective: str = "max_groups"
    completion_exact_groups: int = 0
    completion_allowed_add_widths: tuple[int, ...] = (300, 200, 100, 500)


@dataclass
class ItemType:
    article: str
    width_cm: int
    height_cm: int
    panels_per_unit: int
    len_per_unit_cm: float
    qty: int


def parse_size(text: str, default_height_cm: int) -> tuple[int, int]:
    match = SIZE_RE.search(str(text))
    if not match:
        return 0, default_height_cm
    width = int(match.group(1))
    height = int(match.group(2)) or default_height_cm
    return width, height


def panels_from_width(width_cm: int) -> int:
    return int(round(width_cm / 100))


def unit_len_cm(panels: int, height_cm: int, settings: Settings) -> float:
    if settings.length_mode == "per_panel":
        return panels * (height_cm + settings.job_gap_cm)
    return (
        panels * height_cm
        + max(0, panels - 1) * settings.panel_gap_cm
        + settings.top_bottom_margin_cm
    )


def target_range_cm(settings: Settings) -> tuple[float, float]:
    """Return the usable accepted range, capped by the physical roll length."""
    lower_cm = max(0.0, settings.target_min_m - settings.lower_soft_margin_m) * 100
    upper_m = min(settings.roll_length_m, settings.target_min_m + settings.upper_tolerance_m)
    return lower_cm, upper_m * 100


def roll_overhead_cm(settings: Settings) -> float:
    if not settings.include_leader_trailer_in_target:
        return 0.0
    return settings.leader_cm + settings.trailer_cm


def best_combo_for_height_bucket(
    types: list[ItemType],
    settings: Settings,
    use_min_threshold: bool = True,
) -> dict[str, Any] | None:
    if not types:
        return None

    overhead_cm = roll_overhead_cm(settings)
    accepted_min_cm, accepted_max_cm = target_range_cm(settings)
    content_max_cm = accepted_max_cm - overhead_cm
    if content_max_cm <= 0:
        return None

    content_min_cm = max(0.0, accepted_min_cm - overhead_cm) if use_min_threshold else 0.0
    min_length = math.ceil(content_min_cm * LENGTH_SCALE - 1e-9)
    max_length = math.floor(content_max_cm * LENGTH_SCALE + 1e-9)
    if max_length <= 0 or max_length < min_length:
        return None

    # Bounded knapsack. A dictionary keeps the search sparse and also supports
    # the non-default "combined" mode, where equal panel counts can have
    # different lengths.
    reach: dict[int, tuple[int, int, int] | None] = {0: None}
    for index, item in enumerate(types):
        item_length = int(round(item.len_per_unit_cm * LENGTH_SCALE))
        if item_length <= 0:
            continue

        previous_lengths = tuple(reach)
        max_use = min(item.qty, max_length // item_length)
        for previous_length in previous_lengths:
            for count in range(1, max_use + 1):
                next_length = previous_length + count * item_length
                if next_length > max_length:
                    break
                reach.setdefault(next_length, (previous_length, index, count))

    eligible = (length for length in reach if length >= min_length and length > 0)
    best_length = min(eligible, default=None) if use_min_threshold else max(eligible, default=None)
    if best_length is None:
        return None

    counts: dict[str, int] = {}
    total_panels = 0
    current = best_length
    while current > 0:
        predecessor = reach[current]
        if predecessor is None:
            break
        previous_length, index, count = predecessor
        item = types[index]
        counts[item.article] = counts.get(item.article, 0) + count
        total_panels += item.panels_per_unit * count
        current = previous_length

    content_cm = best_length / LENGTH_SCALE
    total_cm = content_cm + overhead_cm
    total_m = total_cm / 100

    return {
        "height": types[0].height_cm,
        "counts": counts,
        "total_panels": total_panels,
        "content_cm": round(content_cm, 2),
        "overhead_cm": round(overhead_cm, 2),
        "total_cm": round(total_cm, 2),
        "total_m": round(total_m, 2),
        "delta_m": round(total_m - settings.target_min_m, 2),
    }


def max_repeats_for_combo(combo: dict[str, Any] | None, types: list[ItemType]) -> int:
    if not combo:
        return 0

    by_article = {item.article: item for item in types}
    repeats = float("inf")
    for article, count in combo["counts"].items():
        item = by_article.get(article)
        if item is None or item.qty < count:
            return 0
        repeats = min(repeats, item.qty // count)
    return int(repeats)


def consume_combo(combo: dict[str, Any], types: list[ItemType], repeats: int) -> None:
    by_article = {item.article: item for item in types}
    for article, count in combo["counts"].items():
        item = by_article[article]
        item.qty = max(0, item.qty - count * repeats)


def completion_advice_for_buckets(
    buckets: dict[int, list[ItemType]],
    settings: Settings,
) -> list[dict[str, Any]]:
    # The desktop application currently leaves this feature unimplemented too.
    # Keep the response field stable until the domain algorithm is defined.
    return []


def make_item_types(images: list[dict[str, Any]], settings: Settings) -> list[ItemType]:
    accumulated: dict[str, int] = {}
    sizes: dict[str, tuple[int, int]] = {}

    for index, image in enumerate(images, start=1):
        article = str(
            image.get("article")
            or image.get("sku")
            or image.get("name")
            or image.get("id")
            or f"image-{index}"
        ).strip() or f"image-{index}"

        width = image.get("width_cm")
        height = image.get("height_cm")
        if width is None or height is None:
            parsed_width, parsed_height = parse_size(
                image.get("size") or article,
                settings.default_height_cm,
            )
            width = parsed_width if width is None else width
            height = parsed_height if height is None else height

        try:
            width_cm = int(width)
            height_cm = int(height)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid size for image '{article}'.") from exc

        if width_cm <= 0:
            raise ValueError(f"Image '{article}' has invalid width_cm.")
        if height_cm <= 0:
            raise ValueError(f"Image '{article}' has invalid height_cm.")

        try:
            qty = int(image.get("qty", image.get("quantity", 1)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid qty for image '{article}'.") from exc

        qty = max(0, qty) if settings.use_qty else 1
        if qty <= 0:
            continue

        size = (width_cm, height_cm)
        previous_size = sizes.get(article)
        if previous_size is not None and previous_size != size:
            raise ValueError(
                f"Image '{article}' is repeated with conflicting sizes: "
                f"{previous_size[0]}x{previous_size[1]} and {width_cm}x{height_cm}."
            )

        accumulated[article] = accumulated.get(article, 0) + qty
        sizes[article] = size

    items: list[ItemType] = []
    for article, qty in accumulated.items():
        width_cm, height_cm = sizes[article]
        panels = panels_from_width(width_cm)
        if panels <= 0:
            raise ValueError(f"Image '{article}' is too narrow to form a panel.")
        items.append(
            ItemType(
                article=article,
                width_cm=width_cm,
                height_cm=height_cm,
                panels_per_unit=panels,
                len_per_unit_cm=unit_len_cm(panels, height_cm, settings),
                qty=qty,
            )
        )
    return items


def response_range_m(settings: Settings) -> list[float]:
    lower_cm, upper_cm = target_range_cm(settings)
    return [round(lower_cm / 100, 2), round(upper_cm / 100, 2)]


def empty_result(settings: Settings) -> dict[str, Any]:
    return {
        "target_m": settings.target_min_m,
        "tolerance_up_m": settings.upper_tolerance_m,
        "range_m": response_range_m(settings),
        "total_m": 0.0,
        "delta_m": 0.0,
        "fits": False,
        "is_ideal": False,
        "items": [],
        "impositions": [],
        "near_groups": [],
        "leftover": [],
        "leftover_items": [],
        "completion_advice": [],
        "settings_used": asdict(settings),
        "grand_total_m": 0.0,
        "summary": {
            "input_units": 0,
            "efficient_units": 0,
            "inefficient_units": 0,
            "unplaced_units": 0,
        },
        "debug": {"items_count": 0},
        "valid_groups": [],
        "leftover_impositions": [],
    }


def make_imposition(
    label: str,
    combo: dict[str, Any],
    repeats: int,
    fits: bool,
) -> dict[str, Any]:
    item_counts = dict(combo["counts"])
    articles = [
        article
        for article, count in item_counts.items()
        for _ in range(count)
    ]
    units_per_repeat = sum(item_counts.values())
    return {
        "label": label,
        "repeat": repeats,
        "articles": articles,
        "item_counts": item_counts,
        "units_per_repeat": units_per_repeat,
        "total_units": units_per_repeat * repeats,
        "total_panels": combo["total_panels"],
        "total_m": combo["total_m"],
        "delta_m": combo["delta_m"],
        "fits": fits,
        "height": combo["height"],
    }


def process_items(all_types: list[ItemType], settings: Settings) -> dict[str, Any]:
    if not all_types:
        return empty_result(settings)

    # Calculation mutates the working quantities. Keep a stable input snapshot
    # for the response and summary.
    original_types = [replace(item) for item in all_types]
    working_types = [replace(item) for item in all_types]

    buckets: dict[int, list[ItemType]] = {}
    for item in working_types:
        buckets.setdefault(item.height_cm, []).append(item)

    total_cm_all = sum(item.qty * item.len_per_unit_cm for item in original_types)
    total_m_all = round(total_cm_all / 100, 2)
    input_units = sum(item.qty for item in original_types)

    impositions: list[dict[str, Any]] = []
    label_index = 1

    while True:
        best_overall = None
        best_bucket_height = None

        for height, types in buckets.items():
            active_types = [item for item in types if item.qty > 0]
            buckets[height] = active_types
            if not active_types:
                continue
            combo = best_combo_for_height_bucket(active_types, settings, use_min_threshold=True)
            if combo is None:
                continue
            if best_overall is None or abs(combo["total_m"] - settings.target_min_m) < abs(
                best_overall["total_m"] - settings.target_min_m
            ):
                best_overall = combo
                best_bucket_height = height

        if best_overall is None or best_bucket_height is None:
            break

        repeats = max_repeats_for_combo(best_overall, buckets[best_bucket_height])
        if repeats <= 0:
            break

        impositions.append(make_imposition(f"I{label_index}", best_overall, repeats, True))
        consume_combo(best_overall, buckets[best_bucket_height], repeats)
        label_index += 1

    efficient_units = sum(group["total_units"] for group in impositions)
    remaining_after_efficient = sum(
        item.qty for types in buckets.values() for item in types if item.qty > 0
    )
    fits = bool(impositions)
    is_ideal = fits and remaining_after_efficient == 0

    leftover_impositions: list[dict[str, Any]] = []
    label_index = 1

    while True:
        best_combo = None
        best_bucket_height = None

        for height, types in buckets.items():
            active_types = [item for item in types if item.qty > 0]
            buckets[height] = active_types
            if not active_types:
                continue
            combo = best_combo_for_height_bucket(active_types, settings, use_min_threshold=False)
            if combo is None:
                continue
            if best_combo is None or combo["total_cm"] > best_combo["total_cm"]:
                best_combo = combo
                best_bucket_height = height

        if best_combo is None or best_bucket_height is None:
            break

        repeats = max_repeats_for_combo(best_combo, buckets[best_bucket_height])
        if repeats <= 0:
            break

        leftover_impositions.append(
            make_imposition(f"O-{label_index}", best_combo, repeats, False)
        )
        consume_combo(best_combo, buckets[best_bucket_height], repeats)
        label_index += 1

    leftover_items: list[dict[str, Any]] = []
    for types in buckets.values():
        for item in types:
            if item.qty > 0:
                leftover_items.append(
                    {
                        "article": item.article,
                        "qty": item.qty,
                        "width_cm": item.width_cm,
                        "height_cm": item.height_cm,
                    }
                )

    completion = completion_advice_for_buckets(buckets, settings)
    total_m_head = impositions[0]["total_m"] if impositions else 0.0
    inefficient_units = sum(group["total_units"] for group in leftover_impositions)
    unplaced_units = sum(item["qty"] for item in leftover_items)

    return {
        "target_m": settings.target_min_m,
        "tolerance_up_m": settings.upper_tolerance_m,
        "range_m": response_range_m(settings),
        "total_m": round(total_m_head, 2),
        "delta_m": round(total_m_head - settings.target_min_m, 2),
        "fits": fits,
        "is_ideal": is_ideal,
        "items": [asdict(item) for item in original_types],
        "impositions": impositions,
        "near_groups": [],
        "leftover": [f"{item['article']} - {item['qty']}" for item in leftover_items],
        "leftover_items": leftover_items,
        "leftover_impositions": leftover_impositions,
        "completion_advice": completion,
        "settings_used": asdict(settings),
        "grand_total_m": total_m_all,
        "summary": {
            "input_units": input_units,
            "efficient_units": efficient_units,
            "inefficient_units": inefficient_units,
            "unplaced_units": unplaced_units,
        },
        "debug": {
            "items_count": input_units,
            "ideal_definition": (
                "true when all input units are placed into effective impositions "
                "within range_m"
            ),
        },
        "valid_groups": [],
    }


def process_images(images: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    return process_items(make_item_types(images, settings), settings)
