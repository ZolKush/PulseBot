"""Production configuration validation CLI.

Imports are deliberately deferred until a public wrapper is called.  This is
important for the command-line entrypoint: importing :mod:`app.config` loads
the runtime settings and server inventory, and either of those may be the very
thing this command needs to report as invalid.
"""

from __future__ import annotations

import sys


def _check_json_object(path_value: str, field_name: str) -> list[str]:
    from .config.checks import _check_json_object as implementation

    return implementation(path_value, field_name)


def _check_private_data_permissions(path_value: str, field_name: str) -> list[str]:
    from .config.checks import _check_private_data_permissions as implementation

    return implementation(path_value, field_name)


def _readable_file(path_value: str, field_name: str) -> list[str]:
    from .config.checks import _readable_file as implementation

    return implementation(path_value, field_name)


def _writable_parent(path_value: str, field_name: str) -> list[str]:
    from .config.checks import _writable_parent as implementation

    return implementation(path_value, field_name)


def validate_configuration() -> list[str]:
    from .config.checks import validate_configuration as implementation

    return implementation()


def _safe_error_text(exc: Exception) -> str:
    """Return one printable diagnostic line without a traceback or repr."""
    detail = " ".join(str(exc).split())
    return detail or type(exc).__name__


def main() -> int:
    try:
        from .config.checks import main as implementation

        return implementation()
    except Exception as exc:
        # Runtime configuration is imported while resolving ``config.checks``.
        # Keep this outer guard even though checks.main handles validation-time
        # failures: inventory/settings errors can happen before it is callable.
        print(f"Ошибка конфигурации: {_safe_error_text(exc)}", file=sys.stderr)
        return 1


__all__ = [
    "_check_json_object",
    "_check_private_data_permissions",
    "_readable_file",
    "_writable_parent",
    "main",
    "validate_configuration",
]


if __name__ == "__main__":
    raise SystemExit(main())
