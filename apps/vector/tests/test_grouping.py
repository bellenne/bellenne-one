from app.services.grouping import canonical_report_group


def test_known_groups_are_normalized_from_labels_and_product_hints():
    assert canonical_report_group("фотообои") == "Фотообои"
    assert canonical_report_group("Футболки") == "Футболки"
    assert (
        canonical_report_group(
            "Без категории",
            "Флизелиновые фотообои 3d в спальню",
        )
        == "Фотообои"
    )
    assert (
        canonical_report_group(
            "",
            "Фотофасад для забора фотосетка декоративная",
        )
        == "Фотосетки"
    )


def test_custom_manual_group_is_preserved():
    assert (
        canonical_report_group(
            "Настенная графика",
            "Флизелиновые фотообои",
            is_manual=True,
        )
        == "Настенная графика"
    )
