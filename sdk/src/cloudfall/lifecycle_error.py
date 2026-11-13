"""The lifecycle's error, apart so modules ``lifecycle`` uses can raise it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class LifecycleError(RuntimeError):
    """Fail-fast lifecycle error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """Record the failure code, its detail, and what the answer names."""
        self.code = code
        self.detail = message
        self.context: Mapping[str, object] = dict(context or {})
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {**self.context, "code": self.code, "message": self.detail},
        }
