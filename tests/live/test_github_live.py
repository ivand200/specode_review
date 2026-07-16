import os
from pathlib import Path

import pytest

from review_agent import (
    AgentReview,
    GitHubRepository,
    ReviewContext,
    Reviewer,
    publish_review_result,
)
from review_agent.github import GitHubAppClient


class CleanRunner:
    def run(self, context: ReviewContext) -> AgentReview:
        del context
        return AgentReview(findings=())


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for the live GitHub profile")
    return value


@pytest.mark.live_github
def test_real_github_pr_can_be_reviewed_and_commented(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_GITHUB_E2E") != "1":
        pytest.skip("set RUN_LIVE_GITHUB_E2E=1 to enable the live GitHub profile")

    repository = _required_environment("E2E_GITHUB_REPOSITORY")
    if repository != _required_environment("GITHUB_REPOSITORY"):
        pytest.fail("E2E_GITHUB_REPOSITORY must equal the configured repository")
    if "test" not in repository.casefold():
        pytest.fail("the live GitHub profile requires an explicitly named test repository")

    github = GitHubAppClient(
        repository=repository,
        app_id=int(_required_environment("GITHUB_APP_ID")),
        private_key_path=Path(_required_environment("GITHUB_PRIVATE_KEY_PATH")),
    )
    installation_id = github.repository_installation_id()
    request = github.review_request(
        pr_number=int(_required_environment("E2E_GITHUB_PR_NUMBER")),
        installation_id=installation_id,
    )
    reviewer = Reviewer(
        repository=repository,
        workspace_root=tmp_path / "workspaces",
        runner=CleanRunner(),
        source_repository=GitHubRepository(credentials=github),
    )

    result = reviewer.review(request)
    publish_review_result(result, github, installation_id=installation_id)

    assert result.repository == repository
    assert result.pr_number == request.pr_number
    assert result.diff_range.end_sha == request.head_sha
    assert result.status == "no_important_issues"
