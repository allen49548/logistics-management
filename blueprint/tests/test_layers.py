"""Static guards for the single-direction dependency rule."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPWARD = ("blueprint.business", "blueprint.operations", "blueprint.composition")
BANNED_IN_CORE = (
    "报销",
    "工资",
    "司机",
    "车贷",
    "凭证",
    "月份",
    "reimburse",
    "wage",
    "salary",
    "driver",
    "claim",
    "receipt",
    "intake",
    "period",
    "amount",
)


def _files(layer: str) -> list[Path]:
    return sorted((ROOT / layer).rglob("*.py"))


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


class LayerDependencyTests(unittest.TestCase):
    def test_core_never_imports_upward(self):
        for path in _files("core"):
            for module in _imports(path):
                self.assertFalse(
                    module.startswith(UPWARD),
                    f"{path.relative_to(ROOT)} must not import {module}",
                )

    def test_business_depends_only_on_core(self):
        for path in _files("business"):
            for module in _imports(path):
                if module.startswith("blueprint."):
                    self.assertTrue(
                        module.startswith("blueprint.core"),
                        f"{path.relative_to(ROOT)} must not import {module}",
                    )

    def test_operations_depends_only_on_core(self):
        for path in _files("operations"):
            for module in _imports(path):
                if module.startswith("blueprint."):
                    self.assertTrue(
                        module.startswith("blueprint.core"),
                        f"{path.relative_to(ROOT)} must not import {module}",
                    )

    def test_only_composition_is_allowed_to_assemble(self):
        for layer in ("core", "business", "operations"):
            for path in _files(layer):
                for module in _imports(path):
                    self.assertFalse(
                        module.startswith("blueprint.composition"),
                        f"{path.relative_to(ROOT)} must not import {module}",
                    )

    def test_core_has_no_business_vocabulary(self):
        for path in _files("core"):
            text = path.read_text(encoding="utf-8").lower()
            for word in BANNED_IN_CORE:
                self.assertNotIn(
                    word,
                    text,
                    f"{path.relative_to(ROOT)} contains scenario term: {word}",
                )


if __name__ == "__main__":
    unittest.main()
