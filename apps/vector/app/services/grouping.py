from __future__ import annotations

CANONICAL_GROUPS = (
    ("фотосет", "Фотосетки"),
    ("фотофасад", "Фотосетки"),
    ("фотообо", "Фотообои"),
    ("фооообо", "Фотообои"),
    ("футбол", "Футболки"),
)


def canonical_report_group(
    current_group: str | None,
    *hints: str | None,
    is_manual: bool = False,
) -> str:
    """Return a stable report group without overriding custom manual labels."""

    clean_group = " ".join((current_group or "").split())
    folded_group = clean_group.casefold()

    for marker, canonical in CANONICAL_GROUPS:
        if marker in folded_group:
            return canonical

    if is_manual and clean_group and folded_group != "без категории":
        return clean_group

    candidates = (current_group, *hints)
    folded_hints = " ".join(
        value.casefold()
        for value in candidates
        if value and value.strip()
    )
    for marker, canonical in CANONICAL_GROUPS:
        if marker in folded_hints:
            return canonical

    if clean_group and folded_group != "без категории":
        return clean_group
    return "Без категории"
