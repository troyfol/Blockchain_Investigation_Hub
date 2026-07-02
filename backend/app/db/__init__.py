"""Database layer: connection helper + forward-only migration runner.

``apply_migrations`` / ``read_schema_version`` are exposed lazily so that importing this
package does not eagerly import ``migrate`` — which would trigger a runpy warning when
``migrate`` is run as ``python -m backend.app.db.migrate``.
"""

from .connection import get_connection

__all__ = ["get_connection", "apply_migrations", "read_schema_version",
           "assert_supported_schema", "SchemaTooNewError"]


def __getattr__(name: str):
    if name in ("apply_migrations", "read_schema_version", "assert_supported_schema", "SchemaTooNewError"):
        from . import migrate

        return getattr(migrate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
