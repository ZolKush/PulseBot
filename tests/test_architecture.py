"""Mechanical guardrails for the feature-oriented package layout."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"
TESTS_ROOT = PROJECT_ROOT / "tests"
TOOLS_ROOT = PROJECT_ROOT / "tools"
HARD_MODULE_LIMIT = 400

REMOVED_MODULE_PREFIXES = (
    "app.handlers",
    "app.services",
    "app.monitoring.remote",
)
REMOVED_ROOT_MODULES = {
    "app.help_content",
    "app.logging_setup",
    "app.models",
    "app.service_plan",
    "app.settings",
    "app.single_instance",
    "app.staff",
    "app.validation",
    "app.monitoring.status.ssh",
    "app.monitoring.status.diagnostics",
}
EXPECTED_ROOT_MODULES = {
    "__init__.py",
    "config_check.py",
    "constants.py",
    "launcher.py",
    "main.py",
    "storage.py",
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)


def _resolve_from_import(source: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    package = source.split(".")[:-1]
    keep = len(package) - node.level + 1
    prefix = package[: max(keep, 0)]
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join((*prefix, *suffix))


def _import_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for path in APP_ROOT.rglob("*.py"):
        source = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges.update((source, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                edges.add((source, _resolve_from_import(source, node)))
    return edges


def _internal_import_cycle() -> list[str]:
    modules = {_module_name(path) for path in APP_ROOT.rglob("*.py")}
    graph = {module: set() for module in modules}
    for source, target in _import_edges():
        if source in modules and target in modules and source != target:
            graph[source].add(target)

    visited: set[str] = set()
    active: set[str] = set()
    stack: list[str] = []

    def visit(module: str) -> list[str]:
        visited.add(module)
        active.add(module)
        stack.append(module)
        for target in graph[module]:
            if target not in visited:
                cycle = visit(target)
                if cycle:
                    return cycle
            elif target in active:
                start = stack.index(target)
                return [*stack[start:], target]
        stack.pop()
        active.remove(module)
        return []

    for module in sorted(modules):
        if module not in visited:
            cycle = visit(module)
            if cycle:
                return cycle
    return []


def test_python_modules_stay_readable() -> None:
    violations = {
        path.relative_to(PROJECT_ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for source_root in (APP_ROOT, TESTS_ROOT, TOOLS_ROOT)
        for path in source_root.rglob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > HARD_MODULE_LIMIT
    }

    assert violations == {}


def test_removed_catch_all_namespaces_are_not_reintroduced() -> None:
    assert not (APP_ROOT / "handlers").exists()
    assert not (APP_ROOT / "services").exists()

    illegal = {
        (source, target)
        for source, target in _import_edges()
        if target in REMOVED_ROOT_MODULES or target.startswith(REMOVED_MODULE_PREFIXES)
    }
    assert illegal == set()


def test_internal_modules_have_no_direct_import_cycles() -> None:
    assert _internal_import_cycle() == []


def test_application_root_remains_thin() -> None:
    root_modules = {path.name for path in APP_ROOT.glob("*.py")}

    assert root_modules == EXPECTED_ROOT_MODULES
