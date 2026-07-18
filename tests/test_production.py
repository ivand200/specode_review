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


def _settings(tmp_path: Path, *, max_concurrent_reviews: int = 1) -> ProductionSettings:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir()
    return ProductionSettings.from_environment(
        {
            "GITHUB_REPOSITORY": "octo-org/example",
            "GITHUB_APP_ID": "1234",
            "GITHUB_PRIVATE_KEY_PATH": str(private_key),
            "GITHUB_WEBHOOK_SECRET": "a" * 32,
            "CODEX_MODEL": "gpt-5.4",
            "OPENAI_REASONING_EFFORT": "high",
            "REVIEW_KIT_PATH": str(review_kit),
            "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "MAX_CONCURRENT_REVIEWS": str(max_concurrent_reviews),
        }
    )


class RecordingReadiness:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def check(self, settings: ProductionSettings) -> None:
        self._events.append("readiness")
        settings.attempt.workspace_root.mkdir()


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
