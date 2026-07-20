import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from review_agent.configuration import ProductionSettings
from review_agent.github import (
    CHECK_RUN_NAME,
    CheckRun,
    CheckRunPresentation,
    CheckRunStatus,
    ReviewIdentity,
)
from review_agent.models import ReviewRequest
from review_agent.ownership import RepositoryOwnership, RepositoryOwnershipError
from review_agent.production import create_production_app
from review_agent.readiness import StartupReadinessError


def _settings(
    tmp_path: Path,
    *,
    max_concurrent_reviews: int = 1,
    repository: str = "octo-org/example",
) -> ProductionSettings:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir(exist_ok=True)
    return ProductionSettings.from_environment(
        {
            "GITHUB_REPOSITORY": repository,
            "GITHUB_APP_ID": "1234",
            "GITHUB_PRIVATE_KEY_PATH": str(private_key),
            "GITHUB_WEBHOOK_SECRET": "a" * 32,
            "CODEX_MODEL": "gpt-5.4",
            "OPENAI_REASONING_EFFORT": "high",
            "REVIEW_KIT_PATH": str(review_kit),
            "STATE_ROOT": str(tmp_path / "state"),
            "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "MAX_CONCURRENT_REVIEWS": str(max_concurrent_reviews),
        }
    )


class RecordingReadiness:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def check(self, settings: ProductionSettings) -> None:
        self._events.append("readiness")
        settings.attempt.workspace_root.mkdir(exist_ok=True)


class RecordingSandboxResources:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def list_names(self) -> tuple[str, ...]:
        self._events.append("sweep")
        return (f"review-agent-{'f' * 32}",)

    def remove(self, name: str) -> None:
        assert name == f"review-agent-{'f' * 32}"
        self._events.append("remove_stale")


class EmptySandboxResources:
    def list_names(self) -> tuple[str, ...]:
        return ()

    def remove(self, name: str) -> None:
        raise AssertionError(name)


class RecordingGitHub:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._check_runs: list[CheckRun] = []

    def repository_installation_id(self) -> int:
        self._events.append("installation")
        return 23

    def close(self) -> None:
        self._events.append("github_close")

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        del installation_id
        return tuple(
            check_run
            for check_run in self._check_runs
            if check_run.head_sha == identity.head_sha
            and check_run.external_id == identity.external_id
        )

    def create_check_run(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> CheckRun:
        del installation_id
        check_run = CheckRun.model_validate(
            {
                "id": len(self._check_runs) + 101,
                "name": CHECK_RUN_NAME,
                "head_sha": identity.head_sha,
                "external_id": identity.external_id,
                "status": CheckRunStatus.QUEUED,
                "conclusion": None,
                "app": {"id": 1234},
                "output": {"title": "Review queued", "summary": "Queued."},
            }
        )
        self._check_runs.append(check_run)
        return check_run

    def get_check_run(self, *, check_run_id: int, installation_id: int) -> CheckRun:
        del installation_id
        return next(check_run for check_run in self._check_runs if check_run.id == check_run_id)

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
        raise AssertionError((pr_number, installation_id))

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
        return (
            check_run.app.id == 1234
            and check_run.name == CHECK_RUN_NAME
            and check_run.head_sha == identity.head_sha
            and check_run.external_id == identity.external_id
        )

    def update_check_run(
        self,
        *,
        check_run_id: int,
        installation_id: int,
        presentation: CheckRunPresentation,
    ) -> CheckRun | None:
        del check_run_id, installation_id, presentation
        return None


class LockAssertingGitHub(RecordingGitHub):
    def __init__(self, events: list[str], settings: ProductionSettings) -> None:
        super().__init__(events)
        self._settings = settings

    def close(self) -> None:
        def contend() -> None:
            contender = RepositoryOwnership.acquire(self._settings.state)
            contender.close()

        with pytest.raises(RepositoryOwnershipError, match="repository_lock"):
            contend()
        self._events.append("github_close_while_locked")


class SecretFailingGitHub(RecordingGitHub):
    def repository_installation_id(self) -> int:
        message = "secret-token response body"
        raise RuntimeError(message)


def test_production_finishes_readiness_and_one_sweep_before_lifespan_yields(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    settings = _settings(tmp_path)
    app = create_production_app(
        settings=settings,
        environment={"PATH": "/usr/bin"},
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        github_client=RecordingGitHub(events),
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert events == ["readiness", "sweep", "remove_stale", "installation"]

    asyncio.run(exercise())

    assert events == [
        "readiness",
        "sweep",
        "remove_stale",
        "installation",
        "github_close",
    ]


def test_repository_lock_precedes_sweep_and_is_released_after_shutdown(tmp_path: Path) -> None:
    first_events: list[str] = []
    second_events: list[str] = []
    settings = _settings(tmp_path)
    first_app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(first_events),
        sandbox_client=RecordingSandboxResources(first_events),
        github_client=RecordingGitHub(first_events),
    )
    second_app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(second_events),
        sandbox_client=RecordingSandboxResources(second_events),
        github_client=RecordingGitHub(second_events),
    )

    async def exercise() -> None:
        async with first_app.router.lifespan_context(first_app):
            with pytest.raises(StartupReadinessError, match="repository_lock"):
                async with second_app.router.lifespan_context(second_app):
                    pytest.fail("same-repository contender entered its lifespan")
            assert second_events == []
        third_events: list[str] = []
        third_app = create_production_app(
            settings=settings,
            readiness=RecordingReadiness(third_events),
            sandbox_client=RecordingSandboxResources(third_events),
            github_client=RecordingGitHub(third_events),
        )
        async with third_app.router.lifespan_context(third_app):
            assert third_events == ["readiness", "sweep", "remove_stale", "installation"]

    asyncio.run(exercise())


def test_invalid_state_root_is_a_normalized_startup_failure_before_sweep(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    settings = _settings(tmp_path)
    settings.state.root.mkdir(mode=0o755)
    app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        github_client=RecordingGitHub(events),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("invalid state root entered its lifespan")
        assert failure.value.stage == "state_root"

    asyncio.run(exercise())

    assert events == []


def test_distinct_repository_apps_can_enter_lifespan_together(tmp_path: Path) -> None:
    first = create_production_app(
        settings=_settings(tmp_path, repository="octo-org/first"),
        readiness=RecordingReadiness([]),
        sandbox_client=EmptySandboxResources(),
        github_client=RecordingGitHub([]),
    )
    second = create_production_app(
        settings=_settings(tmp_path, repository="octo-org/second"),
        readiness=RecordingReadiness([]),
        sandbox_client=EmptySandboxResources(),
        github_client=RecordingGitHub([]),
    )

    async def exercise() -> None:
        async with (
            first.router.lifespan_context(first),
            second.router.lifespan_context(second),
        ):
            pass

    asyncio.run(exercise())


def test_startup_failure_is_normalized_and_releases_all_acquired_resources(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        github_client=SecretFailingGitHub(events),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("failed startup entered its lifespan")
        assert failure.value.stage == "github_installation"
        assert "secret-token" not in str(failure.value)

    asyncio.run(exercise())

    assert events == ["readiness", "sweep", "remove_stale", "github_close"]
    with RepositoryOwnership.acquire(settings.state):
        pass


def test_invalid_reconciliation_state_prevents_readiness_and_releases_ownership(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with RepositoryOwnership.acquire(settings.state) as ownership:
        outbox = ownership.repository_root / "check-run-outbox-v1"
        outbox.mkdir(mode=0o700)
        (outbox / "untrusted-entry").write_text("secret", encoding="utf-8")
    events: list[str] = []
    app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        github_client=RecordingGitHub(events),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("invalid reconciliation state entered its lifespan")
        assert failure.value.stage == "check_run_outbox"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 503

    asyncio.run(exercise())

    assert events == [
        "readiness",
        "sweep",
        "remove_stale",
        "installation",
        "github_close",
    ]
    with RepositoryOwnership.acquire(settings.state):
        pass


def test_invalid_active_attempt_state_prevents_readiness_and_releases_ownership(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with RepositoryOwnership.acquire(settings.state) as ownership:
        active_attempts = ownership.repository_root / "active-attempts-v1"
        active_attempts.mkdir(mode=0o700)
        (active_attempts / "untrusted-entry").write_text("secret", encoding="utf-8")
    events: list[str] = []
    app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        github_client=RecordingGitHub(events),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("invalid active attempt state entered its lifespan")
        assert failure.value.stage == "active_attempt_state"

    asyncio.run(exercise())

    assert events == [
        "readiness",
        "sweep",
        "remove_stale",
        "installation",
        "github_close",
    ]
    with RepositoryOwnership.acquire(settings.state):
        pass


def test_production_uses_configured_child_capacity_without_a_waiting_queue(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_concurrent_reviews=2)
    started = tmp_path / "started"
    release = tmp_path / "release"
    fixture_child = Path(__file__).parent / "fixtures" / "process_manager_child.py"
    app = create_production_app(
        settings=settings,
        environment={"PATH": "/usr/bin"},
        readiness=RecordingReadiness([]),
        sandbox_client=EmptySandboxResources(),
        github_client=RecordingGitHub([]),
        child_arguments=(
            sys.executable,
            str(fixture_child),
            str(tmp_path / "attempt-command.json"),
            "record-start",
            str(started),
            str(release),
        ),
    )

    def post(client: TestClient, pr_number: int) -> int:
        payload = {
            "action": "opened",
            "installation": {"id": 23},
            "repository": {"full_name": "octo-org/example"},
            "pull_request": {
                "number": pr_number,
                "draft": False,
                "title": "Review",
                "body": "",
                "base": {"sha": "a" * 40},
                "head": {"sha": f"{pr_number:040x}"},
            },
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"a" * 32, body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )
        return response.status_code

    try:
        with TestClient(app) as client:
            assert post(client, 17) == 202
            assert post(client, 18) == 202
            for _ in range(500):
                if started.exists() and len(tuple(started.iterdir())) == 2:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("two production child attempts did not start")
            assert post(client, 19) == 503
            release.touch()
    finally:
        release.touch()


def test_shutdown_stops_pull_request_admission_before_waiting_and_releases_lock_last(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    started = tmp_path / "started"
    release = tmp_path / "release"
    fixture_child = Path(__file__).parent / "fixtures" / "process_manager_child.py"
    app = create_production_app(
        settings=settings,
        environment={"PATH": "/usr/bin"},
        readiness=RecordingReadiness(events),
        sandbox_client=EmptySandboxResources(),
        github_client=LockAssertingGitHub(events, settings),
        child_arguments=(
            sys.executable,
            str(fixture_child),
            str(tmp_path / "attempt-command.json"),
            str(started),
            str(release),
        ),
    )

    initial_payload = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Review",
            "body": "",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }
    async def post(
        client: httpx.AsyncClient,
        event: str,
        payload: dict[str, object],
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"a" * 32, body, hashlib.sha256).hexdigest()
        return await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": signature,
            },
        )

    async def exercise() -> None:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await post(client, "pull_request", initial_payload)).status_code == 202
            for _ in range(500):
                if started.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("production child attempt did not start")

            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            for _ in range(500):
                if (await client.get("/health/ready")).status_code == 503:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("readiness did not drop when shutdown began")

            initial = await post(client, "pull_request", initial_payload | {"pull_request": {
                **initial_payload["pull_request"],
                "number": 18,
                "head": {"sha": "c" * 40},
            }})
            assert initial.json() == {"detail": "review service is shutting down"}
            release.touch()
            await shutdown

    try:
        asyncio.run(exercise())
    finally:
        release.touch()

    assert events[-1] == "github_close_while_locked"
    with RepositoryOwnership.acquire(settings.state):
        pass
