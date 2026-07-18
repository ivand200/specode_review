import logging
import os
import uuid
from collections.abc import Mapping
from typing import Protocol

import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.core import CandidateAcceptance, GitHubRepository, Reviewer
from review_agent.github import GitHubAppClient
from review_agent.readiness import ProductionReadiness
from review_agent.resources import ReviewResourceManager
from review_agent.sandbox import (
    CodexSandboxAdapter,
    DockerSandboxClient,
)
from review_agent.web import create_app
from review_agent.worker import SingleReviewWorker


class ReadinessCheck(Protocol):
    def check(self, settings: ProductionSettings) -> None: ...


def create_production_app(
    *,
    settings: ProductionSettings | None = None,
    environment: Mapping[str, str] | None = None,
    readiness: ReadinessCheck | None = None,
) -> FastAPI:
    resolved_settings = settings or ProductionSettings.from_environment(
        os.environ if environment is None else environment
    )
    (readiness or ProductionReadiness()).check(resolved_settings)

    webhook = resolved_settings.webhook
    attempt = resolved_settings.attempt
    runtime = attempt.runtime
    sandbox_client = DockerSandboxClient(
        config=runtime.sandbox_operation,
    )
    resource_manager = ReviewResourceManager(
        workspace_root=attempt.workspace_root,
        sandbox_prefix=runtime.sandbox_name_prefix,
        sandbox_client=sandbox_client,
    )
    resource_manager.sweep_stale()
    resources = resource_manager.for_attempt(uuid.uuid4().hex)
    adapter = CodexSandboxAdapter(
        client=sandbox_client,
        resources=resources,
        kit=attempt.review_kit_path,
        config=runtime.codex_execution,
    )
    candidate_acceptance = CandidateAcceptance(
        adapter=adapter,
        max_bytes=runtime.candidate_output_max_bytes,
    )
    github = GitHubAppClient(
        repository=webhook.repository,
        app_id=attempt.app_id,
        private_key_path=attempt.private_key_path,
    )
    try:
        reviewer = Reviewer(
            repository=webhook.repository,
            resources=resources,
            candidate_acceptance=candidate_acceptance,
            source_repository=GitHubRepository(credentials=github),
            limits=runtime.review_limits,
        )
    except BaseException:
        github.close()
        raise
    return create_app(
        repository=webhook.repository,
        webhook_secret=webhook.secret,
        manager=SingleReviewWorker(
            reviewer=reviewer,
            publisher=github,
            review_timeout_seconds=runtime.review_timeout_seconds,
        ),
        shutdown_callback=github.close,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    uvicorn.run(
        create_production_app(),
        host="0.0.0.0",  # noqa: S104 - production webhook server must be externally reachable.
        port=8000,
        workers=1,
    )
