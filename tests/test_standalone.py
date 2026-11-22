"""Standalone static guards: SPEC.md section Verification.

Three scanners read the package source. Each one exits non-zero on its own bad input.
The forbidden-reference scan and the import scan print JSON on success and on failure.
The lanes and agents tests document required behavior that does not exist yet.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LITERAL_TERMS = ("board.db", "switchboard", "slots.json", "ownership.json", "tangram-dev",
                 "ILLM_STATE_DIR", "ILLM_HOME")
WORD_TERMS = ("sb", "todo", "assign", "illm", "rswarm", "ticket")
LITERAL_RE = re.compile("|".join(re.escape(term) for term in LITERAL_TERMS))
WORD_RE = re.compile(r"(?<![\w])(" + "|".join(WORD_TERMS) + r")(?![\w])", re.IGNORECASE)
# `decision.` and `/decisions` stay valid. A bare `decision` identifier is forbidden.
DECISION_RE = re.compile(r"(?<![\w/])decision(?![\w.])", re.IGNORECASE)
PROVIDER_CLIS = ("claude", "codex", "agy")
LOCAL_LAND_CALLS = ("fast_forward_main", "squash_merge_tree", "commit_tree")


def package_files(root: Path) -> list[Path]:
    return sorted((root / "jbi").rglob("*.py"))


def forbidden_references(root: Path) -> list[dict]:
    """Every forbidden text in every package file, comments included."""
    found = []
    for path in package_files(root):
        rel = str(path.relative_to(root))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = [m.group(0) for m in LITERAL_RE.finditer(line)]
            hits += [m.group(1) for m in WORD_RE.finditer(line)]
            hits += [m.group(0) for m in DECISION_RE.finditer(line)]
            for hit in hits:
                found.append({"file": rel, "line": number, "term": hit})
    return found


def scan_imports(root: Path) -> dict:
    """Every import in every package file, including try blocks and function bodies."""
    imports, third_party = [], []
    for path in package_files(root):
        rel = path.relative_to(root)
        package = list(rel.parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                    prefix = ".".join(base + ([node.module] if node.module else []))
                    modules = [prefix] if node.module else [f"{prefix}.{a.name}" for a in node.names]
                else:
                    modules = [node.module or ""]
            for module in modules:
                record = {"file": str(rel), "line": node.lineno, "module": module}
                imports.append(record)
                top = module.split(".")[0]
                if top != "jbi" and top not in sys.stdlib_module_names:
                    third_party.append(record)
    return {"imports": imports, "third_party": third_party}


def lanes_landing(root: Path) -> list[str]:
    """Direct landing code in lanes.py, and local merge calls in supervisor.py."""
    problems = []
    lanes = ast.parse((root / "jbi" / "lanes.py").read_text(encoding="utf-8"))
    for node in ast.walk(lanes):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name in LOCAL_LAND_CALLS:
            problems.append(f"lanes.py:{node.lineno}: direct landing call {name}")
    supervisor = ast.parse((root / "jbi" / "supervisor.py").read_text(encoding="utf-8"))
    for node in ast.walk(supervisor):
        if isinstance(node, ast.Attribute) and node.attr in ("submit", "merge") \
                and isinstance(node.value, ast.Attribute) and node.value.attr == "lanes":
            problems.append(f"supervisor.py:{node.lineno}: Supervisor invokes local merge")
    return problems


def agents_dispatch(root: Path) -> list[str]:
    """Direct provider CLI construction in agents.py."""
    source = (root / "jbi" / "agents.py").read_text(encoding="utf-8")
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value in PROVIDER_CLIS:
            problems.append(f"agents.py:{node.lineno}: direct {node.elts[0].value} CLI command")
    if "claude-mux-lite" not in source:
        problems.append("agents.py: final dispatch does not use claude-mux-lite")
    return problems


def main(argv: list[str]) -> int:
    """Run one scanner on a root. Exit non-zero on a finding."""
    mode, root = argv[0], Path(argv[1])
    if mode == "--forbidden":
        found = forbidden_references(root)
        print(json.dumps({"forbidden": found}))
        return 1 if found else 0
    if mode == "--imports":
        result = scan_imports(root)
        print(json.dumps(result))
        return 1 if result["third_party"] else 0
    raise SystemExit(f"unknown mode {mode}")


def run_scanner(mode: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                          capture_output=True, text=True)


class TestStandaloneGuards(unittest.TestCase):
    def test_package_has_no_forbidden_reference(self):
        found = forbidden_references(ROOT)
        detail = "\n".join(f"{h['file']}:{h['line']}: {h['term']}" for h in found)
        self.assertEqual(found, [], "forbidden external references:\n" + detail)

    def test_imports_stay_internal_or_standard_library(self):
        result = scan_imports(ROOT)
        print(json.dumps(result["third_party"]))
        self.assertTrue(result["imports"], "the scan found no import")
        self.assertEqual(result["third_party"], [])

    def test_relative_module_import_is_internal(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "jbi"
            package.mkdir()
            (package / "a.py").write_text("try:\n    from . import metrics\nexcept ImportError:\n    pass\n")
            result = scan_imports(Path(tmp))
            self.assertEqual([r["module"] for r in result["imports"]], ["jbi.metrics"])
            self.assertEqual(result["third_party"], [])

    def test_import_scan_exits_non_zero_on_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "jbi"
            package.mkdir()
            (package / "a.py").write_text("def f():\n    import requests\n")
            run = run_scanner("--imports", Path(tmp))
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(json.loads(run.stdout)["third_party"][0]["module"], "requests")

    def test_forbidden_scan_exits_non_zero_on_bad_input(self):
        cases = {"sb": "sb, rest = glob.split()\n", "comment": "# call illm here\n",
                 "path": "P = 'board.db'\n", "decision": "decision = 1\n"}
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / "jbi"
                package.mkdir()
                (package / "a.py").write_text(text)
                run = run_scanner("--forbidden", Path(tmp))
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(json.loads(run.stdout)["forbidden"][0]["line"], 1)

    def test_forbidden_scan_allows_local_decision_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "jbi"
            package.mkdir()
            (package / "a.py").write_text("x = 'decision.answer'\ny = f'/decisions/{n}'\n")
            self.assertEqual(run_scanner("--forbidden", Path(tmp)).returncode, 0)

    @unittest.expectedFailure
    def test_lanes_has_no_direct_landing_and_supervisor_has_no_local_merge(self):
        # Required missing behavior: ci-conductor lands every merge (SPEC.md).
        self.assertEqual(lanes_landing(ROOT), [])

    @unittest.expectedFailure
    def test_agents_dispatch_uses_claude_mux_lite_only(self):
        # Required missing behavior: final dispatch uses claude-mux-lite only (SPEC.md).
        self.assertEqual(agents_dispatch(ROOT), [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
