from app.core import Settings, process_images


def test_all_units_fit_into_effective_rolls() -> None:
    result = process_images(
        [{"article": "ONE_100x270", "width_cm": 100, "height_cm": 270, "qty": 34}],
        Settings(),
    )

    assert result["is_ideal"] is True
    assert result["fits"] is True
    assert result["items"][0]["qty"] == 34
    assert result["impositions"][0]["repeat"] == 2
    assert result["impositions"][0]["item_counts"] == {"ONE_100x270": 17}
    assert result["impositions"][0]["total_m"] == 48.45
    assert result["summary"] == {
        "input_units": 34,
        "efficient_units": 34,
        "inefficient_units": 0,
        "unplaced_units": 0,
    }


def test_inefficient_remainder_makes_delivery_non_ideal() -> None:
    result = process_images(
        [{"article": "ONE_100x270", "width_cm": 100, "height_cm": 270, "qty": 18}],
        Settings(),
    )

    assert result["is_ideal"] is False
    assert result["fits"] is True
    assert result["summary"]["efficient_units"] == 17
    assert result["summary"]["inefficient_units"] == 1
    assert result["leftover_impositions"][0]["total_m"] == 2.85


def test_duplicate_article_with_conflicting_size_is_rejected() -> None:
    images = [
        {"article": "same", "width_cm": 100, "height_cm": 270, "qty": 1},
        {"article": "same", "width_cm": 200, "height_cm": 270, "qty": 1},
    ]

    try:
        process_images(images, Settings())
    except ValueError as exc:
        assert "conflicting geometry" in str(exc)
    else:
        raise AssertionError("Conflicting sizes must be rejected")


def test_roll_length_caps_the_accepted_upper_bound() -> None:
    result = process_images(
        [{"article": "ONE_100x270", "width_cm": 100, "height_cm": 270, "qty": 17}],
        Settings(roll_length_m=48.0),
    )

    assert result["range_m"] == [47.0, 48.0]
    assert result["fits"] is False
    assert result["is_ideal"] is False


def test_combined_mode_uses_its_own_length_formula() -> None:
    settings = Settings(
        roll_length_m=12.0,
        target_min_m=11.4,
        upper_tolerance_m=0.0,
        length_mode="combined",
        panel_gap_cm=10.0,
        top_bottom_margin_cm=20.0,
    )
    result = process_images(
        [{"article": "TWO_200x270", "width_cm": 200, "height_cm": 270, "qty": 2}],
        settings,
    )

    assert result["is_ideal"] is True
    assert result["impositions"][0]["total_m"] == 11.4


def test_leader_and_trailer_can_be_included_in_target_length() -> None:
    settings = Settings(
        roll_length_m=10.0,
        target_min_m=10.0,
        upper_tolerance_m=0.0,
        job_gap_cm=0.0,
        leader_cm=50.0,
        trailer_cm=50.0,
        include_leader_trailer_in_target=True,
    )
    result = process_images(
        [{"article": "ONE_100x100", "width_cm": 100, "height_cm": 100, "qty": 9}],
        settings,
    )

    assert result["is_ideal"] is True
    assert result["impositions"][0]["total_m"] == 10.0


def test_explicit_running_lengths_with_shared_group_form_one_ideal_roll() -> None:
    settings = Settings(
        roll_length_m=8.3,
        target_min_m=8.3,
        upper_tolerance_m=0.0,
        job_gap_cm=15.0,
    )
    result = process_images(
        [
            {
                "article": "SHORT_100x270",
                "width_cm": 100,
                "height_cm": 270,
                "running_length_cm": 300,
                "calculation_group": "mes-batch",
                "qty": 1,
            },
            {
                "article": "LONG_400x250",
                "width_cm": 400,
                "height_cm": 250,
                "running_length_cm": 500,
                "calculation_group": "mes-batch",
                "qty": 1,
            },
        ],
        settings,
    )

    assert result["is_ideal"] is True
    assert result["impositions"][0]["total_m"] == 8.3
    assert result["impositions"][0]["item_counts"] == {
        "SHORT_100x270": 1,
        "LONG_400x250": 1,
    }
