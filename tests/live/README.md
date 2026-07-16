# Live GitHub profile

This opt-in profile uses the configured GitHub App and dedicated test repository to read
one real pull request, clone its base repository, fetch `refs/pull/<number>/head`, review
the exact API-reported head with the deterministic clean runner, and create one real
top-level pull-request comment. It does not use Docker Sandboxes, OpenAI authentication,
or model budget.

The repository name must contain `test`, and the explicit E2E repository must equal the
application's configured repository. Normal `pytest` runs skip this profile.

Run it only against a disposable, open, non-draft pull request:

```bash
set -a
source .env
set +a
RUN_LIVE_GITHUB_E2E=1 \
E2E_GITHUB_REPOSITORY=ivand200/test_repo \
E2E_GITHUB_PR_NUMBER=<number> \
uv run pytest tests/live/test_github_live.py -q
```

The profile leaves the created review comment in the test pull request as its observable
result. Remove that comment manually when it is no longer useful.
