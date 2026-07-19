import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_agent.configuration import ProductionSettings
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
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert events == ["readiness", "sweep", "remove_stale"]

    asyncio.run(exercise())

    assert events == ["readiness", "sweep", "remove_stale"]


def test_repository_lock_precedes_sweep_and_is_released_after_shutdown(tmp_path: Path) -> None:
    first_events: list[str] = []
    second_events: list[str] = []
    settings = _settings(tmp_path)
    first_app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(first_events),
        sandbox_client=RecordingSandboxResources(first_events),
    )
    second_app = create_production_app(
        settings=settings,
        readiness=RecordingReadiness(second_events),
        sandbox_client=RecordingSandboxResources(second_events),
    )

    async def exercise() -> None:
        async with first_app.router.lifespan_context(first_app):
            with pytest.raises(StartupReadinessError, match="repository_lock"):
                async with second_app.router.lifespan_context(second_app):
                    pytest.fail("same-repository contender entered its lifespan")
            assert second_events == ["readiness"]
        async with second_app.router.lifespan_context(second_app):
            assert second_events == ["readiness", "sweep", "remove_stale"]

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
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("invalid state root entered its lifespan")
        assert failure.value.stage == "state_root"

    asyncio.run(exercise())

    assert events == ["readiness"]


def test_distinct_repository_apps_can_enter_lifespan_together(tmp_path: Path) -> None:
    first = create_production_app(
        settings=_settings(tmp_path, repository="octo-org/first"),
        readiness=RecordingReadiness([]),
        sandbox_client=EmptySandboxResources(),
    )
    second = create_production_app(
        settings=_settings(tmp_path, repository="octo-org/second"),
        readiness=RecordingReadiness([]),
        sandbox_client=EmptySandboxResources(),
    )

    async def exercise() -> None:
        async with (
            first.router.lifespan_context(first),
            second.router.lifespan_context(second),
        ):
            pass

    asyncio.run(exercise())


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
