"""The filesystem contract a migration runs against.

``cloudfall migrate`` and its steps read the project, the engine and every
evidence directory through one ``AgentConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cloudfall.lifecycle import (
    EngineContext,
    StepRunner,
    subprocess_step,
)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Filesystem contract for one agent-facing server instance."""

    project_directory: Path
    schema_directory: Path
    engine_directory: Path
    inventory_file: Path
    observed_directory: Path
    service_observed_directory: Path
    deployments_directory: Path
    releases_directory: Path
    artifacts_directory: Path
    data_migrations_directory: Path = Path("tmp/data-migrations")
    backups_directory: Path = Path("tmp/backups")
    secrets_directory: Path = Path("secrets")
    environment_directory: Path = Path("tmp/env")
    environment_receipts_directory: Path = Path("tmp/env-receipts")
    proposals_directory: Path = Path("tmp/operator/proposals")
    gateway_ca_path: Path | None = None
    gateway_certificate_path: Path | None = None
    gateway_key_path: Path | None = None
    run: StepRunner = subprocess_step
    """Starts each engine step: ``subprocess`` here, ``ctx.run`` under treaty."""

    def context(self) -> EngineContext:
        """Return the engine execution context shared by mutating tools."""
        return EngineContext(
            project_directory=self.project_directory,
            schema_directory=self.schema_directory,
            engine_directory=self.engine_directory,
            inventory_file=self.inventory_file,
            run=self.run,
        )
