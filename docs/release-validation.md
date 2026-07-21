# Signed end-to-end release validation

This cost-bearing campaign validates a release through the real GitHub webhook, Docker Sandbox,
Codex, publication, lifecycle evidence, and cleanup path. Run it only against a dedicated
repository whose name contains `test`.

## Preconditions

Before starting:

1. Complete the network-free and packaging checks.
2. Install the exact release and run `scripts/verify-install.sh`.
3. Keep the service otherwise idle.
4. Install the GitHub App on the test repository with the documented permissions.
5. Confirm that its webhook URL equals `PUBLIC_WEBHOOK_URL`.
6. Authenticate `gh` as an operator allowed to close and reopen the fixture pull request.

Prepare one fresh, open, non-draft pull request without the `no-review` label. Its accepted
revision must add an obvious stable defect on a changed line, such as rejecting age 18 with
`return age > 18`.

Include distinct hostile repository-instruction and tool-configuration markers to prove they do
not override application policy. Record the exact base SHA, head SHA, changed path, and changed
line:

```bash
gh pr view 42 --repo example-org/specode-review-test \
  --json baseRefOid,headRefOid,files
```

## Run the campaign

Run the separately installed command as an operator able to read the system journal and fixed
workspace directory. If privilege elevation is needed, preserve only the narrowly scoped
`GH_TOKEN` used for the close and reopen operations:

```bash
sudo --preserve-env=GH_TOKEN \
  /opt/specode-review/.venv/bin/specode-review-real-e2e \
  --repository example-org/specode-review-test \
  --pr-number 42 \
  --base-sha <40-character-base-sha> \
  --head-sha <40-character-head-sha> \
  --expected-finding "age 18" \
  --expected-path campaign-fixtures/release/adult_age.py \
  --expected-line 4 \
  --forbid-repository-text specode-review-e2e-instruction-release \
  --forbid-repository-text specode-review-e2e-config-release \
  --forbid-log-text "return age > 18"
```

The command requires a ready installed service, the configured public ngrok health endpoint, a
matching GitHub App webhook URL, a fresh exact revision, and zero owned resources. It closes and
reopens the pull request: `closed` is ineligible, while GitHub delivers the eligible signed
`reopened` event through the normal public webhook.

It then waits up to 21 minutes for exactly one SpeCodeReview-owned exact-revision comment. Success
requires:

- the expected finding at the exact changed path and line;
- `blocking` or `important` severity;
- no hostile marker in the result;
- correlated admission, cleanup, publication, and terminal journal evidence;
- zero owned Sandboxes and workspaces.

Output is one bounded JSON result containing only the pass state, comment ID, and attempt ID.

## Inspect and clean up

The campaign deliberately leaves the review comment, pull request, and branch for human
inspection. Afterwards, delete the campaign comment if desired, close the pull request, and delete
its fixture branch. If interrupted after the close operation, reopen the pull request manually
before deciding whether to rerun at a fresh revision.

On failure, inspect only bounded lifecycle records:

```bash
sudo journalctl -u specode-review --since today -o cat
sudo -u specode-review sbx ls --quiet
sudo find /var/lib/specode-review/workspaces -mindepth 1 -maxdepth 1 -print
```

Do not accept a missing, duplicate, wrong-revision, ungrounded, semantically missed, timed-out,
technically failed, redaction-failed, or resource-unclean result. Prepare a fresh revision before
rerunning because an accepted exact-revision comment is deliberately idempotent.
