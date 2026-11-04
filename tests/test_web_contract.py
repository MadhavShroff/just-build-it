"""Static contract checks for the browser dashboard in web/.

The dashboard has no build step, so these checks read the files as they ship:
every referenced file exists, the page runs under the server CSP, nothing loads
from the network, every command the dashboard sends is in the protocol catalog,
only `api.js` talks HTTP, and `patch.js` applies JSON Patch exactly like
`jbi/jsonpatch.py`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
JS = WEB / "js"
TEXT_SUFFIXES = {".html", ".css", ".js", ".svg", ".json", ".txt", ".webmanifest"}
ALLOWED_URLS = ("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink")


def js_files() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def text_files() -> list[Path]:
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)


class _Page(HTMLParser):
    """Collects references, script tags and inline handlers from index.html."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.scripts: list[dict] = []
        self.handlers: list[str] = []
        self._script: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name in ("src", "href"):
            if values.get(name):
                self.refs.append(values[name])
        self.handlers += [f"<{tag} {name}>" for name, _ in attrs if re.fullmatch(r"on[a-z]+", name)]
        if tag == "script":
            self._script = {"src": values.get("src"), "body": ""}
            self.scripts.append(self._script)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script["body"] += data


def parse_index() -> _Page:
    page = _Page()
    page.feed((WEB / "index.html").read_text(encoding="utf-8"))
    return page


def local(ref: str) -> str | None:
    """The path part of a relative reference, or None for anchors, data and other schemes."""
    if ref.startswith(("#", "data:", "mailto:", "javascript:")) or re.match(r"^[a-z][a-z0-9+.-]*:", ref):
        return None
    return ref.split("#", 1)[0].split("?", 1)[0] or None


IMPORT = re.compile(r"""(?:\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?|\bexport\s+[\w*{}\s,$]+\s+from\s+|\bimport\()\s*['"]([^'"]+)['"]""")
SEND = re.compile(r"""\bsend\(\s*['"]([a-z_]+\.[a-z_]+)['"]""")


class TestWebContract(unittest.TestCase):

    def test_index_references_exist(self) -> None:
        self.assertTrue((WEB / "index.html").is_file(), "web/index.html is missing")
        page = parse_index()
        for ref in page.refs:
            path = local(ref)
            if path is None:
                continue
            target = (WEB / path.lstrip("/")).resolve()
            self.assertTrue(target.is_file(), f"index.html references {ref!r}, which does not exist")
        for source in js_files():
            for spec in IMPORT.findall(source.read_text(encoding="utf-8")):
                self.assertTrue(spec.startswith("."), f"{source.name} imports a bare specifier {spec!r}")
                target = (source.parent / spec).resolve()
                self.assertTrue(target.is_file(), f"{source.relative_to(WEB)} imports {spec!r}, which does not exist")

    def test_no_inline_scripts(self) -> None:
        page = parse_index()
        self.assertTrue(page.scripts, "index.html loads no script")
        for script in page.scripts:
            self.assertTrue(script["src"], "index.html has an inline <script> without src")
            self.assertEqual(script["body"].strip(), "", f"<script src={script['src']!r}> has an inline body")
        self.assertEqual(page.handlers, [], "index.html uses inline event handler attributes")
        for source in js_files():
            text = source.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\beval\s*\(", f"{source.name} calls eval")
            self.assertNotRegex(text, r"\bnew\s+Function\s*\(", f"{source.name} builds code with new Function")

    def test_no_external_urls(self) -> None:
        url = re.compile(r"https?://[^\s'\"()<>]+")
        for path in text_files():
            for match in url.findall(path.read_text(encoding="utf-8")):
                if not match.startswith(ALLOWED_URLS):
                    self.fail(f"{path.relative_to(WEB)} references the network: {match}")

    def test_commands_exist(self) -> None:
        from jbi.protocol import COMMANDS
        used: dict[str, str] = {}
        for source in js_files():
            for ctype in SEND.findall(source.read_text(encoding="utf-8")):
                used.setdefault(ctype, source.name)
        self.assertTrue(used, "web/js sends no command with send('<type>', ...)")
        # A command type can also reach send() through a helper, for example
        # payloadFor(schema, 'run.configure', ...). Check every literal in a command namespace.
        from jbi.protocol import EVENTS
        spaces = "|".join(sorted({name.split(".")[0] for name in COMMANDS}))
        literal = re.compile(rf"""['"`]((?:{spaces})\.[a-z_]+)['"`]""")
        for source in js_files():
            for ctype in literal.findall(source.read_text(encoding="utf-8")):
                if ctype not in EVENTS:
                    used.setdefault(ctype, source.name)
        unknown = {ctype: name for ctype, name in used.items() if ctype not in COMMANDS}
        self.assertEqual(unknown, {}, f"commands that are not in jbi.protocol.COMMANDS: {unknown}")

    def test_css_never_truncates_text(self) -> None:
        # Product rule: the UI never truncates text. Text wraps; ids and paths break anywhere.
        rule = re.compile(r"text-overflow\s*:\s*ellipsis|(?:-webkit-)?line-clamp\s*:", re.IGNORECASE)
        sheets = sorted((WEB / "css").glob("*.css"))
        self.assertTrue(sheets, "web/css holds no stylesheet")
        for sheet in sheets:
            for number, line in enumerate(sheet.read_text(encoding="utf-8").splitlines(), 1):
                match = rule.search(line)
                self.assertIsNone(match, f"{sheet.relative_to(WEB)}:{number} truncates text "
                                         f"({match and match.group(0)}); let the text wrap instead")

    def test_api_is_only_http_module(self) -> None:
        pattern = re.compile(r"\bfetch\s*\(|\bEventSource\s*\(|\bXMLHttpRequest\b|\bnavigator\.sendBeacon\b|\bWebSocket\s*\(")
        for source in js_files():
            if source == JS / "api.js":
                continue
            match = pattern.search(source.read_text(encoding="utf-8"))
            self.assertIsNone(match, f"{source.relative_to(WEB)} talks HTTP ({match and match.group(0)}); "
                                     "only js/api.js may")

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_patch_js_matches_jsonpatch(self) -> None:
        from jbi import jsonpatch
        vectors = [
            ("add object key", {"tasks": {"a": {"status": "ready"}}},
             [{"op": "add", "path": "/tasks/b", "value": {"status": "blocked"}}]),
            ("add array index and append", {"timeline": [1, 3]},
             [{"op": "add", "path": "/timeline/1", "value": 2}, {"op": "add", "path": "/timeline/-", "value": 4}]),
            ("remove array element", {"timeline": ["a", "b", "c"], "x": {"y": 1}},
             [{"op": "remove", "path": "/timeline/0"}, {"op": "remove", "path": "/x/y"}]),
            ("replace nested value", {"plan": {"tasks": {"core": {"status": "ready", "stages": {"implement":
                                                                                           {"status": "pending"}}}}}},
             [{"op": "replace", "path": "/plan/tasks/core/stages/implement/status", "value": "running"},
              {"op": "replace", "path": "/plan/tasks/core/status", "value": "running"}]),
            ("RFC 6901 escaping", {"nodes": {"a/b": 1, "m~n": 2}},
             [{"op": "replace", "path": "/nodes/a~1b", "value": 10}, {"op": "add", "path": "/nodes/m~0n", "value": 20},
              {"op": "add", "path": "/nodes/~01", "value": 30}]),
            ("root replace", {"old": True},
             [{"op": "replace", "path": "", "value": {"run": {"phase": "intake"}, "list": [1, {"deep": [2]}]}}]),
        ]
        failures = [
            ("replace of a missing key", {"a": {}}, [{"op": "replace", "path": "/a/missing", "value": 1}]),
            ("remove past the end", {"a": [1]}, [{"op": "remove", "path": "/a/3"}]),
        ]
        expected = [jsonpatch.apply(copy.deepcopy(doc), ops) for _, doc, ops in vectors]
        for name, doc, ops in failures:
            with self.assertRaises(jsonpatch.PatchError, msg=name):
                jsonpatch.apply(copy.deepcopy(doc), ops)

        script = """
            import { pathToFileURL } from 'node:url';
            const mod = await import(pathToFileURL(process.argv[1]).href);
            const input = JSON.parse(process.argv[2]);
            const run = ([name, doc, ops]) => {
              try {
                const result = mod.applyPatch(doc, ops);
                if (ops.some(op => op.path === '') && result === ops.find(op => op.path === '').value) {
                  return { name, error: 'root replace returned the operation value itself, not a copy' };
                }
                return { name, result };
              } catch (error) {
                return { name, error: error instanceof mod.PatchError ? 'PatchError' : String(error) };
              }
            };
            console.log(JSON.stringify({ ok: input.ok.map(run), bad: input.bad.map(run) }));
        """
        payload = json.dumps({"ok": vectors, "bad": failures})
        done = subprocess.run(["node", "--input-type=module", "-e", script, str(JS / "patch.js"), payload],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, f"node failed: {done.stderr.strip()}")
        output = json.loads(done.stdout)
        for (name, _, _), want, got in zip(vectors, expected, output["ok"]):
            self.assertNotIn("error", got, f"{name}: patch.js raised {got.get('error')}")
            self.assertEqual(got["result"], want, f"{name}: patch.js and jsonpatch.py disagree")
        for (name, _, _), got in zip(failures, output["bad"]):
            self.assertEqual(got.get("error"), "PatchError", f"{name}: patch.js must throw PatchError, got {got}")


if __name__ == "__main__":
    unittest.main()
