# Tickets: Code Review Agent V0.1

Build a bounded, single-worker GitHub code-review service that reviews the exact accepted pull-request revision inside a disposable Docker Sandbox and publishes only a validated result. Source: [Code Review Agent V0.1 specification](spec-code-review-agent-v0-1.md).

Work the **frontier**: any ticket whose blockers are all done. Tickets 4, 5, 6, and 7 expose useful parallel work once their blockers are complete.

## [x] Review an exact local PR revision through the typed Interface

**What to build:** A caller can submit a trusted `ReviewRequest` to `review(ReviewRequest) -> ReviewResult` and receive a clean typed result for the exact accepted pull-request revision. The Module materializes a controlled local Git repository in a unique workspace, checks out the accepted head commit, computes the merge base once, gives the resulting immutable diff range and changed-path manifest to a fake review runner, and cleans up the workspace on every terminal path.

**Blocked by:** None — can start immediately.

- [x] The project is runnable with `uv`, and normal tests require no GitHub, Docker, OpenAI credentials, or network access.
- [x] Immutable, bounded Pydantic models cover `ReviewRequest`, `DiffRange`, `AgentReview`, `Finding`, `Location`, and `ReviewResult` with the identities and limits declared in the specification.
- [x] One public core Interface, `review(ReviewRequest) -> ReviewResult`, accepts its dependencies at construction without exposing clone, diff, validation, or cleanup stages to callers.
- [x] A controlled diverged Git repository proves that the Module checks out `ReviewRequest.head_sha` in detached-head mode and computes one merge base from the accepted base and head commits.
- [x] The same immutable `DiffRange` is supplied to the changed-path manifest, fake runner input, and returned result; its end SHA equals the accepted head SHA.
- [x] An empty valid `AgentReview` produces a `ReviewResult` whose application-derived status is `no_important_issues`.
- [x] Unique temporary workspaces under the dedicated workspace root are removed after success, repository failure, runner failure, validation failure, timeout/cancellation, and unexpected exceptions.
- [x] Expected core failures use normalized categories and are never converted into a successful empty review.

## [x] Publish only grounded findings as deterministic Markdown

**What to build:** A review with valid important findings travels from the fake runner through structural and repository grounding validation into a deterministic, safely rendered top-level comment. A clean review produces an explicit no-important-issues comment, while any invalid candidate fails the whole attempt and produces no publishable result.

**Blocked by:** Review an exact local PR revision through the typed Interface.

- [x] The fake runner can return zero to five ordered findings with only `blocking` or `important` severity and all declared string and collection bounds enforced.
- [x] Every finding has one to three locations, at least one location names a changed path, and all paths remain inside the frozen checkout.
- [x] Grounding accepts files present at the reviewed head and deleted changed paths, and validates supplied one-based lines against referenced head text files.
- [x] Grounding rejects traversal, absolute paths, symlink escapes, nonexistent paths, out-of-range lines, binary line references, and findings with no changed-path location.
- [x] Parsing, schema, or grounding failure rejects the complete candidate immediately; invalid findings are neither dropped nor converted into a clean result.
- [x] Application code derives `issues_found` only when validated findings exist and copies repository, PR, and commit identity only from trusted request and diff values.
- [x] Deterministic Markdown includes the automated-review notice, exact `start_sha..end_sha`, derived status, and each finding's severity, title, locations, evidence, impact, and suggested fix.
- [x] Model-authored strings cannot inject mentions, HTML, hidden markers, or application-owned headings, and every valid comment remains below GitHub's size limit.
- [x] A fake publisher captures exactly one top-level comment for both findings and clean results and captures no comment for a failed review.

## [x] Turn a signed GitHub webhook into an asynchronous review comment

**What to build:** A running FastAPI application accepts a correctly signed, eligible GitHub pull-request webhook, converts it into a `ReviewRequest`, enqueues it, returns without waiting for review work, and lets its lifespan-owned worker produce one captured comment using fake external adapters.

**Blocked by:** Publish only grounded findings as deterministic Markdown.

- [x] The handler reads the raw body and verifies `X-Hub-Signature-256` with constant-time comparison before parsing trusted fields or enqueueing work.
- [x] Only `X-GitHub-Event: pull_request`, action `opened`, a non-draft PR, and the configured canonical repository are eligible; all other valid events are successful no-ops.
- [x] Invalid signatures return an authentication error, malformed eligible payloads return a client error, and neither path enqueues work or exposes secrets or internal exceptions.
- [x] An eligible payload maps repository, PR number, installation ID, exact base/head SHAs, title, and bounded description into a typed `ReviewRequest`.
- [x] The request receives `202 Accepted` only after `put_nowait` succeeds; clone, model, and publication work never execute in the request handler.
- [x] Application lifespan owns one bounded queue and exactly one consumer task, without introducing persistence, retries, or delivery deduplication.
- [x] Duplicate valid deliveries may enqueue duplicate reviews and comments, matching the explicit V0.1 limitation.
- [x] **E2E checkpoint A — local webhook product path:** start the service on a real local socket, send signed GitHub-shaped HTTP requests without `TestClient` or a direct ASGI call, and assert the actual status/body plus eventual fake-publisher comment.
- [x] Checkpoint A also exercises actual HTTP responses for invalid signature, ignored event, malformed eligible payload, and queue-full behavior.

## [x] Use GitHub App credentials for exact checkout and publication

**What to build:** The product path can authenticate as the configured GitHub App, materialize the exact accepted revision from the configured base repository—including fork pull requests—and create one real top-level pull-request comment without exposing or persisting credentials.

**Blocked by:** Turn a signed GitHub webhook into an asynchronous review comment.

- [x] A narrow GitHub adapter obtains an ephemeral installation credential using the configured App identity and private key and scopes operations to the configured repository.
- [x] Repository materialization clones the configured base repository, fetches the accepted base commit and `refs/pull/<number>/head`, verifies the event's exact head commit exists, and checks it out detached.
- [x] A moved branch or newer PR head cannot replace `ReviewRequest.head_sha`, and failure to obtain either commit or their merge base fails the review.
- [x] Fork PRs work through the base repository's GitHub pull-request head ref without assuming the source branch exists in that repository.
- [x] Installation credentials never remain in clone URLs, Git configuration, prompts, logs, exceptions, sandbox mounts, or webhook responses.
- [x] The production publisher posts exactly one deterministic top-level comment after a successful review and performs no inline review, approval, requested-change, merge-state, or prior-comment update operation.
- [x] Mocked GitHub API contract tests cover authentication, credential failure, repository reads, comment creation, response failure, permission failure, and secret redaction without network access.
- [x] A real local-Git test proves checkout, manifest, runner input, grounding, typed result, and rendered comment all use the same merge-base-to-exact-head range.
- [x] **E2E checkpoint B prerequisite:** an opt-in profile can use a dedicated GitHub test repository and real GitHub App request/responses while substituting the deterministic fake Codex runner.

## [x] Reject reviews that exceed trusted input and output bounds

**What to build:** Expensive or unbounded reviews fail before inference or publication, while permitted reviews carry visibly bounded untrusted context through the same webhook-to-result path.

**Blocked by:** Turn a signed GitHub webhook into an asynchronous review comment.

- [x] A deterministic manifest counts changed paths and added/deleted text lines from the immutable diff; binary files count toward the file limit but not the text-line limit.
- [x] More than 100 changed files or more than 5,000 changed text lines produces a `review_too_large` failure before the first runner/model request or sandbox creation.
- [x] Pull-request descriptions are limited to 10,000 characters with a visible truncation marker, while titles and all request fields enforce their declared limits.
- [x] Captured subprocess diagnostics, candidate result bytes, Pydantic strings and collections, rendered comments, sandbox resources, and configured process output are bounded.
- [x] Limit failures are logged with normalized context and publish no partial, clean, or potentially misleading comment.
- [x] Tests prove each boundary at its exact limit and immediately beyond it, including binary changes and multibyte text where byte and character limits differ.
- [x] Configuration and documentation claim only limits the implementation can enforce; unsupported Codex request or tool-call limits are not presented as guarantees.

## [x] Keep the single worker bounded and failure-isolated

**What to build:** A burst or failed attempt cannot create parallel review work, block the worker forever, or prevent later queued requests from completing. Operators receive bounded HTTP behavior and normalized diagnostics while the in-memory, non-durable V0.1 contract remains explicit.

**Blocked by:** Turn a signed GitHub webhook into an asynchronous review comment.

- [x] The queue capacity is exactly ten `ReviewRequest` values, preserves FIFO order, and returns `503 Service Unavailable` without claiming acceptance when full.
- [x] Scheduler tests prove no more than one invocation of the review Interface—including publication—is active at a time.
- [x] One configured deadline, defaulting to 15 minutes, covers credential acquisition, materialization, sandbox work, validation, publication, and normal cleanup; every operation receives only its remaining time.
- [x] Review, validation, timeout, cancellation, and publication failures publish no final comment for that attempt, call `task_done`, and do not stop later queued work.
- [x] Failures are logged with repository, PR number, exact head SHA, stage, and normalized error category, without credentials, source content, prompt contents, or raw model output.
- [x] Graceful shutdown stops accepting new work and gives the active review a bounded opportunity to finish; abrupt termination and lost queued/active work remain documented limitations.
- [x] Cleanup remains in the review Module's `finally` path so a scheduler timeout cannot bypass forced sandbox and workspace removal during normal process operation.
- [x] **E2E checkpoint B — live GitHub transport with controlled review:** run the service on a real local socket, send a signed webhook referencing a real PR in the dedicated test repository, and assert the actual `202` response, exact accepted head SHA, real GitHub App clone/fetch and comment request/responses, and expected PR comment from the fake runner.
- [x] Checkpoint B is opt-in, isolated to the dedicated repository, records created external resources for cleanup, and requires no Docker Sandbox, OpenAI authentication, or model budget.

## [x] Prove disposable sandbox lifecycle without a model call

**What to build:** The service can create and destroy a uniquely named Docker Sandbox around a frozen review checkout while proving the host checkout is read-only, the VM-local working copy is writable and exact, and no state crosses review attempts. This integration slice does not call a model.

**Blocked by:** Review an exact local PR revision through the typed Interface.

- [x] Each attempt creates one sandbox under the application-owned naming prefix and mounts only the frozen checkout read-only plus a request-specific application control directory read-write.
- [x] Setup copies the checkout into VM-local writable storage and verifies its `HEAD` equals `ReviewRequest.head_sha` before any autonomous execution could start.
- [x] The sandbox applies configured CPU, memory, process/output, and deny-by-default network limits without placing GitHub or raw OpenAI credentials in its filesystem or environment.
- [x] Success, setup failure, command failure, timeout, cancellation, and validation failure all attempt `sbx rm --force` and host-workspace cleanup from a `finally` path.
- [x] The host checkout's content and Git state are unchanged after mutation and deletion inside the VM-local copy.
- [x] Two sequential attempts use distinct fresh sandboxes and prove that a marker written by the first is absent from the second.
- [x] Startup sweeping removes only abandoned sandboxes and workspace directories matching both the dedicated root/prefix and the application's strict naming convention.
- [x] The no-model integration test exercises forced timeout cleanup and orphan sweeping and is separately marked from normal tests that require no Docker runtime.

## [ ] Return a typed Codex review from the isolated sandbox

**What to build:** The production `CodexSandboxRunner` invokes Codex once inside a disposable microVM using trusted application-owned review policy, then returns only a bounded schema-constrained candidate to the existing validation, grounding, result, and publication path.

**Blocked by:** Publish only grounded findings as deterministic Markdown; Reject reviews that exceed trusted input and output bounds; Keep the single worker bounded and failure-isolated; Prove disposable sandbox lifecycle without a model call.

- [ ] The pinned application-owned mixin kit supplies root instructions, `.agents/skills/code-review`, required local tooling, and OpenAI-only network policy and passes `sbx kit validate`.
- [ ] Codex starts from the application-owned control workspace, ignores user configuration and rules, loads no repository-provided hooks, skills, MCP declarations, `AGENTS.md`, or `.codex` configuration, and treats repository content as untrusted data.
- [ ] The runner uses non-interactive, ephemeral, schema-constrained Codex execution with internal approvals/sandboxing bypassed only inside the outer Docker Sandbox boundary.
- [ ] Trusted inputs include the fixed diff range, deterministic changed-path manifest, review policy, and bounded untrusted PR title/description; neither the model nor repository can redefine revisions, publication, network policy, or capabilities.
- [ ] Codex can inspect, mutate, build, test, and execute only within the disposable VM-local copy, with network access restricted to OpenAI through Docker's host-managed OAuth credential proxy.
- [ ] The runner captures bounded JSONL diagnostics and a bounded final artifact, returns only the candidate `AgentReview`, and normalizes CLI exit, output, limit, and sandbox failures.
- [ ] Candidate parsing, Pydantic validation, deterministic grounding, status derivation, and safe comment rendering reuse the same path proven with the fake runner; there is no corrective rerun or loose-text fallback.
- [ ] Tests prove that a malicious repository-owned instruction cannot alter the trusted control workspace, access credentials, change the fixed revisions, or inject unvalidated publication content.

## [ ] Fail startup safely and verify production readiness

**What to build:** The complete service accepts traffic only when its one-repository GitHub, bounded worker, trusted review kit, Docker Sandbox, and Codex runtime configuration is valid. Operators can run an explicit live test that exercises the full signed-webhook-to-real-comment path and proves the selected isolation contract before rollout.

**Blocked by:** Use GitHub App credentials for exact checkout and publication; Return a typed Codex review from the isolated sandbox.

- [ ] Startup validates the canonical repository, GitHub App settings and secret paths, webhook secret, Codex model, workspace root, review timeout, sandbox resources, subprocess/output bounds, application-owned kit path, and ownership prefixes before accepting traffic.
- [ ] Startup readiness checks the pinned compatible `sbx` and Codex versions, validates the mixin kit, verifies required host capabilities, and reports normalized redacted failures.
- [ ] The example environment and operator documentation match the specification: 15-minute default end-to-end timeout, host-managed OpenAI OAuth/proxying, no `pydantic-ai`, no raw OpenAI credential in the sandbox, and no unenforceable model/tool-call guarantees.
- [ ] Normal CI runs fake-adapter unit/contract tests and the mocked product-flow test without GitHub, Docker, OpenAI credentials, network, or model cost.
- [ ] Docker lifecycle integration and live tests are clearly separated and opt-in, use generated fixture repositories, and cannot target an important working copy by default.
- [ ] Observable logs and errors are checked for GitHub credentials, raw OpenAI credentials, clone credential material, prompt/source contents, and unbounded model diagnostics.
- [ ] **E2E checkpoint C — full live sandboxed review:** run the service on a real local socket and send a signed webhook for a deliberately defective PR in the dedicated GitHub test repository, exercising the real queue, GitHub App, Git operations, Docker Sandbox, Codex CLI, validation, and GitHub publication.
- [ ] Checkpoint C asserts the actual HTTP response, exact reviewed range and expected material finding in the real PR comment, host-checkout immutability, sandbox/workspace removal, trusted-kit loading, repository-config isolation, OpenAI-only networking, and absence of secret leakage.
- [ ] Checkpoint C is an explicit cost-bearing opt-in action requiring dedicated GitHub credentials, Docker Sandbox readiness, host-managed OpenAI authentication, time, and model budget; failure blocks operational rollout rather than changing the core Interface.
