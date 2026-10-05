# SPDX-License-Identifier: Apache-2.0
"""Guards for the design rules in CLAUDE.md that a linter can't check."""

import ast
from pathlib import Path

import pytest

from signal_archive_recorder.modes import ModeRegistry

SRC = Path(__file__).resolve().parents[2] / "src" / "signal_archive_recorder"
# Code allowed to know about specific modes: the registry itself and source adapters.
MODE_AWARE = ("modes", "sources")
CLOCK_FILE = SRC / "core" / "clock.py"
WALL_CLOCK_CALLS = {
    ("time", "time"), ("time", "time_ns"), ("time", "monotonic"), ("time", "monotonic_ns"),
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today"),
}  # fmt: skip


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _rel(path: Path) -> str:
    return str(path.relative_to(SRC))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _mode_names() -> set[str]:
    names = set()
    for mode in ModeRegistry.load_default():
        if mode.id == "unknown":
            continue
        names.add(mode.id.casefold())
        names.add(mode.display_name.casefold())
        for raws in mode.aliases.values():
            names.update(r.casefold() for r in raws)
    return names


@pytest.mark.parametrize(
    "path",
    [p for p in _py_files() if p.relative_to(SRC).parts[0] not in MODE_AWARE],
    ids=_rel,
)
def test_no_mode_names_in_core(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    names = _mode_names()
    hits = [
        f"line {node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and node.value.strip().casefold() in names
    ]
    assert not hits, f"mode names belong in modes.json, not {_rel(path)}: {hits}"


@pytest.mark.parametrize("path", [p for p in _py_files() if p != CLOCK_FILE], ids=_rel)
def test_clock_only_read_in_clock_module(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = [
        f"line {node.lineno}: {node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id, node.attr) in WALL_CLOCK_CALLS
    ]
    assert not hits, f"use core.clock.Clock instead of reading time directly: {hits}"
