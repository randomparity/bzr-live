from __future__ import annotations

import ast
import unittest
from pathlib import Path


PACKAGE = Path(__file__).parents[1] / "src" / "bzr_live" / "scenario"
FORBIDDEN_IMPORTS = {
    "subprocess",
    "socket",
    "http",
    "urllib",
    "sqlite3",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "sqlalchemy",
    "psycopg",
    "pymysql",
    "pymongo",
    "redis",
}


class CapabilityBoundaryTests(unittest.TestCase):
    def test_package_has_no_mutation_or_io_clients(self) -> None:
        violations: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    if roots & FORBIDDEN_IMPORTS:
                        violations.append(f"{path.name}:{node.lineno}: forbidden import")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".", 1)[0] in FORBIDDEN_IMPORTS:
                        violations.append(f"{path.name}:{node.lineno}: forbidden import")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr in {"system", "popen"}
                ):
                    violations.append(f"{path.name}:{node.lineno}: forbidden os call")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
