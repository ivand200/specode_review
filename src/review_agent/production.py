import logging
import os
from collections.abc import Mapping
from typing import Protocol

import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.core import CandidateAcceptance, GitHubRepository, Reviewer
from review_agent.github import GitHubAppClient
from review_agent.readiness import ProductionReadiness
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

    runtime = resolved_settings.runtime
    sandbox_client = DockerSandboxClient(
        config=runtime.sandbox_operation,
    )
    adapter = CodexSandboxAdapter(
        client=sandbox_client,
        sandbox_prefix=runtime.sandbox_name_prefix,
        kit=resolved_settings.review_kit_path,
        config=runtime.codex_execution,
    )
    candidate_acceptance = CandidateAcceptance(
        adapter=adapter,
        max_bytes=runtime.candidate_output_max_bytes,
    )
    github = GitHubAppClient(
        repository=resolved_settings.repository,
        app_id=resolved_settings.app_id,
        private_key_path=resolved_settings.private_key_path,
    )
    try:
        reviewer = Reviewer(
            repository=resolved_settings.repository,
            workspace_root=resolved_settings.workspace_root,
            candidate_acceptance=candidate_acceptance,
            source_repository=GitHubRepository(credentials=github),
            limits=runtime.review_limits,
        )
    except BaseException:
        github.close()
        raise
    return create_app(
        repository=resolved_settings.repository,
        webhook_secret=resolved_settings.webhook_secret,
        worker=SingleReviewWorker(
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
