import logging
import os
from collections.abc import Mapping
from typing import Protocol

import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.process_manager import ReviewProcessManager
from review_agent.readiness import ProductionReadiness
from review_agent.resources import ReviewResourceManager, SandboxResourceClient
from review_agent.sandbox import DockerSandboxClient
from review_agent.web import create_app


class ReadinessCheck(Protocol):
    def check(self, settings: ProductionSettings) -> None: ...


def create_production_app(
    *,
    settings: ProductionSettings | None = None,
    environment: Mapping[str, str] | None = None,
    readiness: ReadinessCheck | None = None,
    sandbox_client: SandboxResourceClient | None = None,
    child_arguments: tuple[str, ...] | None = None,
) -> FastAPI:
    resolved_environment = os.environ if environment is None else environment
    resolved_settings = settings or ProductionSettings.from_environment(resolved_environment)
    (readiness or ProductionReadiness()).check(resolved_settings)

    webhook = resolved_settings.webhook
    attempt = resolved_settings.attempt
    runtime = attempt.runtime
    resolved_sandbox_client = sandbox_client or DockerSandboxClient(
        config=runtime.sandbox_operation
    )
    resource_manager = ReviewResourceManager(
        workspace_root=attempt.workspace_root,
        sandbox_prefix=runtime.sandbox_name_prefix,
        sandbox_client=resolved_sandbox_client,
    )
    resource_manager.sweep_stale()
    manager = ReviewProcessManager(
        attempt_settings=attempt,
        resource_manager=resource_manager,
        parent_environment=resolved_environment,
        child_arguments=child_arguments,
        max_concurrent_reviews=webhook.max_concurrent_reviews,
    )
    return create_app(
        repository=webhook.repository,
        webhook_secret=webhook.secret,
        manager=manager,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    uvicorn.run(
        create_production_app(),
        host="0.0.0.0",  # noqa: S104 - production webhook server must be externally reachable.
        port=8000,
        workers=1,
    )
