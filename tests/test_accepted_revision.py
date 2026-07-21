import pytest

from specode_review.accepted_revision import AcceptedRevision
from specode_review.models import ReviewRequest


def test_direct_construction_stores_canonical_identity_fields() -> None:
    revision = AcceptedRevision(
        repository="Octo-Org/Example",
        pr_number=17,
        base_sha="A" * 40,
        head_sha="B" * 40,
    )

    assert revision.repository == "octo-org/example"
    assert revision.pr_number == 17
    assert revision.base_sha == "a" * 40
    assert revision.head_sha == "b" * 40


def test_review_request_conversion_uses_only_accepted_revision_fields() -> None:
    first = ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="A" * 40,
        head_sha="B" * 40,
        title="Original title",
        description="Original description",
    )
    second = first.model_copy(
        update={
            "repository": "octo-org/example",
            "installation_id": 99,
            "title": "Changed title",
            "description": "Changed description",
        }
    )

    assert AcceptedRevision.from_review_request(first) == AcceptedRevision.from_review_request(
        second
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("repository", "octo-org/other"),
        ("pr_number", 18),
        ("base_sha", "c" * 40),
        ("head_sha", "d" * 40),
    ],
)
def test_each_accepted_revision_field_participates_in_value_identity(
    field: str,
    replacement: object,
) -> None:
    revision = AcceptedRevision(
        repository="octo-org/example",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert revision != revision.model_copy(update={field: replacement})
    assert len({revision, AcceptedRevision.model_validate(revision.model_dump())}) == 1


def test_external_id_preserves_the_persisted_v1_identity() -> None:
    revision = AcceptedRevision(
        repository="Octo-Org/Example",
        pr_number=17,
        base_sha="A" * 40,
        head_sha="B" * 40,
    )

    assert revision.external_id == (
        "specode-review:v1:b3fdc634e74cf30721e4dc24158636348334fa1c133b44a74eb401e89db2119f"
    )
