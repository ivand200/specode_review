from importlib import metadata, util


def test_distribution_exposes_only_specode_review_identity() -> None:
    distribution = metadata.distribution("specode-review")

    assert distribution.metadata["Name"] == "specode-review"
    assert util.find_spec("specode_review") is not None
    assert util.find_spec("review_agent") is None

    scripts = {
        entry_point.name: entry_point
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    assert set(scripts) == {"specode-review", "specode-review-real-e2e"}
    assert callable(scripts["specode-review"].load())
    assert callable(scripts["specode-review-real-e2e"].load())
