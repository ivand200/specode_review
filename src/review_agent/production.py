import asyncio
import logging
import os
from collections.abc import Mapping
from types import TracebackType
from typing import Protocol, Self

import uvicorn
from fastapi import FastAPI

from review_agent.active_attempts import ActiveAttemptStateError, FileActiveAttemptRegistry
from review_agent.configuration import ProductionSettings, SandboxOperationPolicy
from review_agent.coordinator import (
    CheckRunGateway,
    RetryReviewRequest,
    ReviewAttemptCoordinator,
)
from review_agent.github import GitHubAppClient, GitHubError
from review_agent.models import ReviewRequest
from review_agent.ownership import RepositoryOwnership, RepositoryOwnershipError
from review_agent.process_manager import ReviewProcessManager
from review_agent.readiness import ProductionReadiness, StartupReadinessError
from review_agent.reconciliation import (
    CheckRunReconciler,
    CheckRunUpdater,
    ReconciliationStateError,
    ReconciliationTiming,
)
from review_agent.resources import ReviewResourceManager, SandboxResourceClient
from review_agent.sandbox import DockerSandboxClient
from review_agent.submission import SubmissionOutcome
from review_agent.web import create_app


class ReadinessCheck(Protocol):
    def check(self, settings: ProductionSettings) -> None: ...


class ProductionGitHub(CheckRunGateway, CheckRunUpdater, Protocol):
    def repository_installation_id(self) -> int: ...

    def close(self) -> None: ...


class _ProductionCoordinator:
    """Own startup gates and delegate webhook admission to one coordinator."""

    def __init__(  # noqa: PLR0913 - explicit production dependency boundary.
        self,
        *,
        settings: ProductionSettings,
        environment: Mapping[str, str],
        readiness: ReadinessCheck,
        sandbox_client: SandboxResourceClient | None,
        github_client: ProductionGitHub | None,
        child_arguments: tuple[str, ...] | None,
    ) -> None:
        self._settings = settings
        self._environment = environment
        self._readiness = readiness
        self._sandbox_client = sandbox_client
        self._github = github_client
        self._child_arguments = child_arguments
        self._ownership: RepositoryOwnership | None = None
        self._coordinator: ReviewAttemptCoordinator | None = None
        self._coordinator_entered = False
        self._github_active = False

    async def __aenter__(self) -> Self:
        startup_stage = "repository_lock"
        try:
            self._ownership = RepositoryOwnership.acquire(self._settings.state)
            startup_stage = "sandbox_host_readiness"
            self._readiness.check(self._settings)

            startup_stage = "stale_resource_sweep"
            attempt = self._settings.attempt
            sandbox_client = self._sandbox_client or DockerSandboxClient(
                config=SandboxOperationPolicy(
                    process_output_max_bytes=attempt.process_output_max_bytes,
                    cleanup_timeout_seconds=attempt.sandbox_cleanup_timeout_seconds,
                )
            )
            resource_manager = ReviewResourceManager(
                workspace_root=attempt.workspace_root,
                sandbox_prefix=attempt.sandbox_name_prefix,
                sandbox_client=sandbox_client,
            )
            resource_manager.sweep_stale()

            startup_stage = "github_installation"
            github = self._github or GitHubAppClient(
                repository=self._settings.webhook.repository,
                app_id=attempt.app_id,
                private_key_path=attempt.private_key_path,
            )
            self._github = github
            self._github_active = True
            installation_id = await asyncio.to_thread(github.repository_installation_id)

            startup_stage = "coordinator_startup"
            reconciliation = self._settings.reconciliation
            reconciler = CheckRunReconciler(
                repository_root=self._ownership.repository_root,
                repository=self._settings.webhook.repository,
                installation_id=installation_id,
                github=github,
                timing=ReconciliationTiming(
                    periodic_interval_seconds=reconciliation.periodic_interval_seconds,
                    shutdown_timeout_seconds=reconciliation.shutdown_timeout_seconds,
                ),
            )
            process = ReviewProcessManager(
                attempt_settings=attempt,
                resource_manager=resource_manager,
                parent_environment=self._environment,
                child_arguments=self._child_arguments,
            )
            self._coordinator = ReviewAttemptCoordinator(
                github=github,
                process=process,
                reconciler=reconciler,
                active_attempts=FileActiveAttemptRegistry(
                    self._ownership.repository_root,
                    repository=self._settings.webhook.repository,
                ),
                installation_id=installation_id,
                max_concurrent_reviews=self._settings.webhook.max_concurrent_reviews,
            )
            await self._coordinator.__aenter__()
            self._coordinator_entered = True
            return self  # noqa: TRY300 - success ends the startup transaction.
        except RepositoryOwnershipError as error:
            await self._unwind_startup()
            raise StartupReadinessError(error.stage) from None
        except StartupReadinessError:
            await self._unwind_startup()
            raise
        except ReconciliationStateError as error:
            await self._unwind_startup()
            raise StartupReadinessError(error.stage) from None
        except ActiveAttemptStateError as error:
            await self._unwind_startup()
            raise StartupReadinessError(error.stage) from None
        except GitHubError:
            await self._unwind_startup()
            raise StartupReadinessError(startup_stage) from None
        except Exception:  # noqa: BLE001 - normalize every production startup boundary.
            await self._unwind_startup()
            raise StartupReadinessError(startup_stage) from None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._coordinator_entered and self._coordinator is not None:
                await self._coordinator.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._coordinator_entered = False
            self._close_external_resources()

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:
        coordinator = self._coordinator
        if not self._coordinator_entered or coordinator is None:
            return SubmissionOutcome.STOPPING
        return await coordinator.start(request)

    async def retry(self, request: RetryReviewRequest) -> SubmissionOutcome:
        coordinator = self._coordinator
        if not self._coordinator_entered or coordinator is None:
            return SubmissionOutcome.STOPPING
        return await coordinator.retry(request)

    async def _unwind_startup(self) -> None:
        if self._coordinator_entered and self._coordinator is not None:
            await self._coordinator.__aexit__(None, None, None)
        self._coordinator_entered = False
        self._close_external_resources()

    def _close_external_resources(self) -> None:
        try:
            if self._github_active and self._github is not None:
                self._github.close()
                self._github_active = False
        finally:
            if self._ownership is not None:
                self._ownership.close()
                self._ownership = None


def create_production_app(  # noqa: PLR0913 - injectable production assembly seam.
    *,
    settings: ProductionSettings | None = None,
    environment: Mapping[str, str] | None = None,
    readiness: ReadinessCheck | None = None,
    sandbox_client: SandboxResourceClient | None = None,
    github_client: ProductionGitHub | None = None,
    child_arguments: tuple[str, ...] | None = None,
) -> FastAPI:
    resolved_environment = os.environ if environment is None else environment
    resolved_settings = settings or ProductionSettings.from_environment(resolved_environment)
    webhook = resolved_settings.webhook
    coordinator = _ProductionCoordinator(
        settings=resolved_settings,
        environment=resolved_environment,
        readiness=readiness or ProductionReadiness(),
        sandbox_client=sandbox_client,
        github_client=github_client,
        child_arguments=child_arguments,
    )
    return create_app(
        repository=webhook.repository,
        webhook_secret=webhook.secret,
        manager=coordinator,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    uvicorn.run(
        create_production_app(),
        host="0.0.0.0",  # noqa: S104 - production webhook server must be externally reachable.
        port=8000,
        workers=1,
    )
