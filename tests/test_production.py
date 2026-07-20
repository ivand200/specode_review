import asyncio
import hashlib
import hmac
import json
import threading
from pathlib import Path

import httpx
import pytest

from review_agent.configuration import (
    CodexExecutionPolicy,
    ProductionPaths,
    ProductionServiceSettings,
    ReasoningEffort,
)
from review_agent.models import ReviewRequest
from review_agent.production import create_production_app
from review_agent.readiness import StartupReadinessError
from review_agent.review_runner import PreflightOutcome


class SimulatedCleanupError(RuntimeError):
    pass


def _settings(tmp_path: Path, *, max_concurrent_reviews: int = 3) -> ProductionServiceSettings:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir()
    return ProductionServiceSettings(
        app_id=1234,
        webhook_secret="a" * 32,
        public_webhook_url="https://reviews.example/webhooks/github",
        codex_execution=CodexExecutionPolicy(
            model="gpt-5.4",
            reasoning_effort=ReasoningEffort.HIGH,
        ),
        max_concurrent_reviews=max_concurrent_reviews,
        paths=ProductionPaths(
            private_key_path=private_key,
            review_kit_path=review_kit,
            workspace_root=tmp_path / "workspaces",
        ),
    )


class RecordingReadiness:
    def __init__(self, events: list[str], *, failure: bool = False) -> None:
        self._events = events
        self._failure = failure

    def check(self, settings: ProductionServiceSettings) -> None:
        self._events.append("readiness")
        if self._failure:
            stage = "sandbox_host_capability"
            raise StartupReadinessError(stage)
        settings.attempt.workspace_root.mkdir(parents=True)


class RecordingSandboxResources:
    def __init__(self, events: list[str], *, fail_remove: bool = False) -> None:
        self._events = events
        self._fail_remove = fail_remove

    def list_names(self) -> tuple[str, ...]:
        self._events.append("sandbox_list")
        return ("review-agent-" + "f" * 32, "foreign-" + "e" * 32)

    def remove(self, name: str) -> None:
        assert name == "review-agent-" + "f" * 32
        self._events.append("sandbox_remove")
        if self._fail_remove:
            raise SimulatedCleanupError


class ControlledRunner:
    def __init__(self) -> None:
        self.preflights: list[ReviewRequest] = []
        self.runs: list[tuple[ReviewRequest, str]] = []
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.release_run.set()

    def preflight(self, request: ReviewRequest) -> PreflightOutcome:
        self.preflights.append(request)
        return PreflightOutcome.READY

    def run(self, request: ReviewRequest, attempt_id: str) -> object:
        self.runs.append((request, attempt_id))
        self.run_started.set()
        assert self.release_run.wait(5)
        return object()


def _signed_payload(
    *,
    pr_number: int = 17,
    head_sha: str = "b" * 40,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 23},
            "repository": {"full_name": "octo-org/example"},
            "pull_request": {
                "number": pr_number,
                "draft": False,
                "title": "Review",
                "body": "",
                "labels": [],
                "base": {"sha": "a" * 40},
                "head": {"sha": head_sha},
            },
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"a" * 32, body, hashlib.sha256).hexdigest()
    return body, {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": signature,
    }


async def _post_review(client: httpx.AsyncClient, *, pr_number: int = 17) -> httpx.Response:
    body, headers = _signed_payload(pr_number=pr_number, head_sha=f"{pr_number:040x}")
    return await client.post("/webhooks/github", content=body, headers=headers)


def test_production_validates_and_sweeps_before_readiness_then_uses_the_lifecycle(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    runner = ControlledRunner()
    app = create_production_app(
        settings=_settings(tmp_path),
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events),
        runner=runner,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            assert events == ["readiness", "sandbox_list", "sandbox_remove"]
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health/ready")).status_code == 200
                assert (await _post_review(client)).status_code == 202
                assert await asyncio.to_thread(runner.run_started.wait, 5)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503

    asyncio.run(exercise())

    assert len(runner.preflights) == 1
    assert len(runner.runs) == 1


def test_production_fails_closed_when_stale_cleanup_cannot_be_confirmed(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    app = create_production_app(
        settings=_settings(tmp_path),
        readiness=RecordingReadiness(events),
        sandbox_client=RecordingSandboxResources(events, fail_remove=True),
        runner=ControlledRunner(),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("failed cleanup entered the serving lifespan")
        assert failure.value.stage == "stale_resource_sweep"

    asyncio.run(exercise())
    assert events == ["readiness", "sandbox_list", "sandbox_remove"]


def test_production_readiness_failure_prevents_resource_enumeration(tmp_path: Path) -> None:
    events: list[str] = []
    app = create_production_app(
        settings=_settings(tmp_path),
        readiness=RecordingReadiness(events, failure=True),
        sandbox_client=RecordingSandboxResources(events),
        runner=ControlledRunner(),
    )

    async def exercise() -> None:
        with pytest.raises(StartupReadinessError, match="sandbox_host_capability"):
            async with app.router.lifespan_context(app):
                pytest.fail("failed readiness entered the serving lifespan")

    asyncio.run(exercise())
    assert events == ["readiness"]


def test_shutdown_drops_readiness_and_admission_before_draining_accepted_work(
    tmp_path: Path,
) -> None:
    runner = ControlledRunner()
    runner.release_run.clear()
    app = create_production_app(
        settings=_settings(tmp_path),
        readiness=RecordingReadiness([]),
        sandbox_client=RecordingSandboxResources([]),
        runner=runner,
    )

    async def exercise() -> None:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await _post_review(client)).status_code == 202
            assert await asyncio.to_thread(runner.run_started.wait, 5)

            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            for _ in range(100):
                if (await client.get("/health/ready")).status_code == 503:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("readiness did not drop when shutdown began")

            response = await _post_review(client, pr_number=18)
            assert response.status_code == 503
            assert response.json() == {"detail": "review service is shutting down"}
            assert not shutdown.done()
            runner.release_run.set()
            await shutdown

    try:
        asyncio.run(exercise())
    finally:
        runner.release_run.set()
