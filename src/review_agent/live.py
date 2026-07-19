from review_agent.github import GitHubAppClient, derive_review_identity
from review_agent.models import ReviewRequest
from review_agent.publishing import owned_revision_comments


class LiveProfilePreconditionError(Exception):
    """The accepted revision is not safe for a live rollout profile."""


def require_fresh_live_review(
    *,
    request: ReviewRequest,
    github: GitHubAppClient,
) -> None:
    identity = derive_review_identity(request)
    owned_check_runs = tuple(
        check_run
        for check_run in github.list_check_runs(
            identity=identity,
            installation_id=request.installation_id,
        )
        if github.is_owned_check_run(check_run, identity=identity)
    )
    if owned_check_runs:
        message = (
            "live profile requires no application-owned Check Run for this review identity; "
            "manually prepare a fresh accepted base/head revision"
        )
        raise LiveProfilePreconditionError(message)
    if owned_revision_comments(request=request, gateway=github):
        message = (
            "live profile requires no exact-marker application-owned comment for this review "
            "identity; manually prepare a fresh accepted base/head revision"
        )
        raise LiveProfilePreconditionError(message)
