import logging
import os
from collections.abc import Mapping
from types import TracebackType
from typing import Protocol, Self

import uvicorn
from fastapi import FastAPI

from review_agent.configuration import (
    ProductionServiceSettings,
    ReviewLimits,
    SandboxOperationPolicy,
)
from review_agent.github import GitHubAppClient
from review_agent.lifecycle import ReviewLifecycle
from review_agent.models import ReviewRequest
from review_agent.readiness import ProductionReadiness, StartupReadinessError
from review_agent.resources import ReviewResourceManager, SandboxResourceClient
from review_agent.review_runner import PreflightOutcome, ReviewRunner
from review_agent.sandbox import CodexSandboxAdapter, DockerSandboxClient, ReviewExecutionClient
from review_agent.submission import SubmissionOutcome
from review_agent.web import create_app


class ReadinessCheck(Protocol):
    def check(self, settings: ProductionServiceSettings) -> None: ...


class ProductionRunner(Protocol):
    def preflight(self, request: ReviewRequest) -> PreflightOutcome: ...

    def run(self, request: ReviewRequest, attempt_id: str) -> object: ...


class _ProductionLifecycle:
    """Validate, clean, assemble, and own the one in-process review lifecycle."""

    def __init__(
        self,
        *,
        settings: ProductionServiceSettings,
        readiness: ReadinessCheck,
        sandbox_client: SandboxResourceClient | None,
        runner: ProductionRunner | None,
    ) -> None:
        self._settings = settings
        self._readiness = readiness
        self._sandbox_client = sandbox_client
        self._runner = runner
        self._lifecycle: ReviewLifecycle | None = None

    async def __aenter__(self) -> Self:
        self._readiness.check(self._settings)
        attempt = self._settings.attempt
        try:
            sandbox = self._sandbox_client or DockerSandboxClient(
                config=SandboxOperationPolicy(
                    process_output_max_bytes=attempt.process_output_max_bytes,
                    cleanup_timeout_seconds=attempt.sandbox_cleanup_timeout_seconds,
                )
            )
            resources = ReviewResourceManager(
                workspace_root=attempt.workspace_root,
                sandbox_prefix=attempt.sandbox_name_prefix,
                sandbox_client=sandbox,
            )
        except Exception:  # noqa: BLE001 - normalize production adapter construction.
            stage = "production_assembly"
            raise StartupReadinessError(stage) from None
        try:
            resources.sweep_stale()
        except Exception:  # noqa: BLE001 - normalize the startup cleanup boundary.
            stage = "stale_resource_sweep"
            raise StartupReadinessError(stage) from None
        resolved_runner = self._runner
        if resolved_runner is None:
            if not isinstance(sandbox, ReviewExecutionClient):
                stage = "production_assembly"
                raise StartupReadinessError(stage)
            resolved_runner = _build_runner(
                settings=self._settings,
                sandbox=sandbox,
                resources=resources,
            )
        lifecycle = ReviewLifecycle(
            runner=resolved_runner,
            max_concurrent_reviews=self._settings.max_concurrent_reviews,
        )
        await lifecycle.__aenter__()
        self._lifecycle = lifecycle
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        lifecycle = self._lifecycle
        if lifecycle is not None:
            await lifecycle.__aexit__(exc_type, exc_value, traceback)
            self._lifecycle = None

    async def submit(self, request: ReviewRequest) -> SubmissionOutcome:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return SubmissionOutcome.STOPPING
        return await lifecycle.submit(request)


def _build_runner(
    *,
    settings: ProductionServiceSettings,
    sandbox: ReviewExecutionClient,
    resources: ReviewResourceManager,
) -> ReviewRunner:
    attempt = settings.attempt

    def github_client(repository: str) -> GitHubAppClient:
        return GitHubAppClient(
            repository=repository,
            app_id=settings.app_id,
            private_key_path=attempt.private_key_path,
        )

    return ReviewRunner(
        github_client_factory=github_client,
        resource_manager=resources,
        candidate_adapter_factory=lambda attempt_resources: CodexSandboxAdapter(
            client=sandbox,
            resources=attempt_resources,
            kit=attempt.review_kit_path,
            config=attempt.codex_execution,
        ),
        limits=ReviewLimits(
            process_output_max_bytes=attempt.process_output_max_bytes,
            sandbox_resources=attempt.sandbox_resources,
        ),
        candidate_output_max_bytes=attempt.candidate_output_max_bytes,
    )


def create_production_app(
    *,
    settings: ProductionServiceSettings | None = None,
    environment: Mapping[str, str] | None = None,
    readiness: ReadinessCheck | None = None,
    sandbox_client: SandboxResourceClient | None = None,
    runner: ProductionRunner | None = None,
) -> FastAPI:
    resolved_environment = os.environ if environment is None else environment
    resolved_settings = settings or ProductionServiceSettings.from_environment(
        resolved_environment
    )
    lifecycle = _ProductionLifecycle(
        settings=resolved_settings,
        readiness=readiness or ProductionReadiness(),
        sandbox_client=sandbox_client,
        runner=runner,
    )
    return create_app(
        webhook_secret=resolved_settings.webhook_secret,
        lifecycle=lifecycle,
    )


def main() -> None:
    settings = ProductionServiceSettings.from_environment(os.environ)
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
    )
    uvicorn.run(
        create_production_app(settings=settings),
        host="127.0.0.1",
        port=8000,
        workers=1,
    )
