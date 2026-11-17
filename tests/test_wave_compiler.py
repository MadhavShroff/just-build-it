"""Independent CLI tests for the devloop wave compiler reconciled into just-build-it.

Loads `wave_compiler` through `jbi.dag_loader` (imported from
`/home/madhavshroff/src/devloop-cli/wave-compiler`) and runs all 39 tests in-process.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from jbi import dag_loader

wc = dag_loader.wave_compiler
ROOT = dag_loader.WAVE_COMPILER_PATH.parent

HEAD = 'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "auto"\n'


def task(tid, deps=(), owns=()):
    deps = ", ".join(f'"{d}"' for d in deps)
    owns = ", ".join(f'"{o}"' for o in owns)
    return (
        f'  - id: "{tid}"\n'
        f'    title: "Do {tid}"\n'
        f'    goal: "Goal {tid}"\n'
        f'    deps: [{deps}]\n'
        f'    acceptance: "python3 -m unittest"\n'
        f'    owns: [{owns}]\n'
    )


def file_text(tasks_text, head=HEAD):
    return head + "tasks:\n" + tasks_text


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, name, text, binary=False):
        path = self.dir / name
        if binary:
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, argv):
        code, out, err = self.cli(argv)
        self.assertEqual(code, 0, err or out)
        self.assertEqual(err, "")
        return out

    def refuse(self, argv, expect=None):
        code, out, err = self.cli(argv)
        self.assertNotEqual(code, 0, f"expected refusal, got exit 0 and:\n{out}")
        self.assertNotEqual(code, 0)
        self.assertTrue(err.startswith("wave-compiler: error:"), err)
        self.assertNotIn("Traceback", err)
        if expect:
            self.assertIn(expect, err)
        return err


class TestSuccess(Harness):
    def test_sample_file_output(self):
        out = self.ok([str(ROOT / "sample.yaml")])
        self.assertEqual(out, "WAVE 1\ncore READY\nWAVE 2\ncli BLOCKED(core)\n")

    def test_kahn_levels_and_file_order(self):
        text = file_text(task("a") + task("b") + task("c", ["a", "b"]) + task("d", ["a"]))
        out = self.ok([str(self.fixture("dag.yaml", text))])
        self.assertEqual(
            out,
            "WAVE 1\na READY\nb READY\nWAVE 2\nc BLOCKED(a)\nd BLOCKED(a)\n",
        )

    def test_deep_chain_dag_no_recursion(self):
        n = 1500
        body = task("t0") + "".join(task(f"t{i}", [f"t{i-1}"]) for i in range(1, n))
        out = self.ok([str(self.fixture("deep.yaml", file_text(body)))])
        self.assertEqual(out.count("\nWAVE "), n - 1)
        self.assertIn(f"WAVE {n}\nt{n-1} BLOCKED(t{n-2})\n", out)
        self.assertIn("WAVE 1\nt0 READY\n", out)


class TestStates(Harness):
    def setUp(self):
        super().setUp()
        body = (
            task("lib", owns=["src/lib.py"])
            + task("app", ["lib"])
            + task("extra", ["lib"])
            + task("tool", ["app"])
        )
        self.path = self.fixture("states.yaml", file_text(body))

    def test_all_four_states(self):
        out = self.ok([str(self.path), "--merged", "lib", "--running", "app"])
        self.assertEqual(
            out,
            "WAVE 1\nlib MERGED\n"
            "WAVE 2\napp RUNNING\nextra READY\n"
            "WAVE 3\ntool BLOCKED(app)\n",
        )

    def test_blocked_names_first_unmerged_dependency(self):
        body = task("a") + task("b") + task("c", ["b", "a"])
        path = self.fixture("order.yaml", file_text(body))
        out = self.ok([str(path), "--merged", "a"])
        self.assertIn("c BLOCKED(b)\n", out)

    def test_ready_task_does_not_wait_for_its_wave(self):
        out = self.ok([str(self.path), "--merged", "lib"])
        self.assertIn("extra READY\n", out)
        self.assertIn("app READY\n", out)
        self.assertIn("tool BLOCKED(app)\n", out)

    def test_merged_set_accepts_transitive_closure(self):
        body = task("a") + task("b", ["a"]) + task("c", ["b"])
        path = self.fixture("chain.yaml", file_text(body))
        out = self.ok([str(path), "--merged", "a,b,c"])
        self.assertIn("c MERGED\n", out)


class TestContradictions(Harness):
    def setUp(self):
        super().setUp()
        body = task("a") + task("b", ["a"]) + task("c", ["a"])
        self.path = self.fixture("contra.yaml", file_text(body))

    def test_unknown_merged_identifier(self):
        self.refuse([str(self.path), "--merged", "nope"], "unknown task 'nope'")

    def test_unknown_running_identifier(self):
        self.refuse([str(self.path), "--running", "zzz"], "unknown task 'zzz'")

    def test_overlapping_sets(self):
        self.refuse([str(self.path), "--merged", "a,b", "--running", "b"],
                    "both merged and running")

    def test_merged_without_transitive_dependencies(self):
        self.refuse([str(self.path), "--merged", "b"], "lacks merged dependency 'a'")

    def test_running_with_unmerged_dependency(self):
        self.refuse([str(self.path), "--running", "b"], "lacks merged dependency 'a'")


class TestCycles(Harness):
    def test_cycle_yaml_names_closed_cycle_behind_acyclic_node(self):
        code, out, err = self.cli([str(ROOT / "cycle.yaml")])
        self.assertEqual(code, 2)
        self.assertEqual(err, "wave-compiler: error: dependency cycle: lint -> test -> build -> lint\n")
        self.assertEqual(out, "")

    def test_two_node_cycle(self):
        body = task("a", ["b"]) + task("b", ["a"])
        self.refuse([str(self.fixture("two.yaml", file_text(body)))],
                    "dependency cycle: a -> b -> a")

    def test_self_dependency(self):
        body = task("a", ["a"])
        self.refuse([str(self.fixture("self.yaml", file_text(body)))],
                    "task 'a' depends on itself")


class TestMalformed(Harness):
    def test_missing_file(self):
        self.refuse([str(self.dir / "absent.yaml")], "cannot read")

    def test_invalid_utf8(self):
        path = self.fixture("bad.bin", b'project: "d\xffmo"\n', binary=True)
        self.refuse([str(path)], "not valid UTF-8")

    def test_restricted_syntax(self):
        valid = file_text(task("a"))
        cases = {
            "comment": valid + "# a comment\n",
            "tab": valid.replace('mode: "new"', 'mode:\t"new"'),
            "unquoted scalar": valid.replace('project: "t"', "project: t"),
            "anchor": valid.replace('project: "t"', 'project: &p "t"'),
            "alias": valid.replace('repo: "./t"', "repo: *p"),
            "tag": valid.replace('project: "t"', 'project: !!str "t"'),
            "multiline scalar": valid.replace('title: "Do a"', "title: >\n      folded"),
            "trailing content": valid.replace('project: "t"', 'project: "t" extra'),
            "flow mapping": valid.replace('project: "t"', "project: {}"),
            "number value": valid.replace('project: "t"', "project: 7"),
            "unknown root field": valid.replace('mode: "new"', 'mode: "new"\nbogus: "x"'),
            "unknown task field": valid.replace('    goal: "Goal a"',
                                                '    goal: "Goal a"\n    bogus: "x"'),
            "duplicate root key": valid.replace('mode: "new"', 'mode: "new"\nproject: "u"'),
            "duplicate task key": valid.replace('    goal: "Goal a"',
                                                '    title: "Again"\n    goal: "Goal a"'),
            "list elements not strings": valid.replace("deps: []", "deps: [1]"),
            "bad indentation": valid.replace('  - id: "a"', '   - id: "a"'),
            "dash item without tasks header": valid.replace("tasks:\n", ""),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.refuse([str(self.fixture(f"{name.replace(' ', '_')}.yaml", text))])


class TestSchema(Harness):
    def refuse_text(self, text, expect, waves="auto", groups=None):
        head = f'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "{waves}"\n'
        if groups is not None:
            head += f"wave_groups: {groups}\n"
        self.refuse([str(self.fixture("schema.yaml", head + "tasks:\n" + text))], expect)

    def test_missing_root_fields(self):
        self.refuse([str(self.fixture("bare.yaml", 'project: "t"\ntasks:\n' + task("a")))],
                    "root field 'repo'")

    def test_empty_scalar(self):
        self.refuse_text(task("a").replace('"Do a"', '""'), "must be non-empty")

    def test_bad_mode(self):
        head = 'project: "t"\nrepo: "./t"\nmode: "fast"\nwaves: "auto"\n'
        self.refuse([str(self.fixture("mode.yaml", head + "tasks:\n" + task("a")))],
                    "mode must be 'new' or 'existing'")

    def test_bad_waves(self):
        head = 'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "manual"\n'
        self.refuse([str(self.fixture("waves.yaml", head + "tasks:\n" + task("a")))],
                    "waves must be 'auto' or 'explicit'")

    def test_empty_task_list(self):
        self.refuse([str(self.fixture("empty.yaml", HEAD + "tasks:\n"))],
                    "supply at least one task")

    def test_bad_task_id_pattern(self):
        self.refuse_text(task("9bad"), "invalid task id '9bad'")

    def test_duplicate_task_id(self):
        self.refuse_text(task("a") + task("a"), "duplicate task id 'a'")

    def test_repeated_dependency(self):
        self.refuse_text(task("a") + task("b", ["a", "a"]), "repeats a dependency")

    def test_unknown_dependency(self):
        self.refuse_text(task("a", ["ghost"]), "depends on unknown task 'ghost'")

    def test_owns_path_rules(self):
        cases = [("/abs/path.py", "owns invalid path"), ("src\\\\win.py", "owns invalid path"),
                 ("src/../up.py", "owns invalid path"), ("", "owns invalid path")]
        for path, expect in cases:
            with self.subTest(path=path):
                self.refuse_text(task("a", owns=[path]), expect)

    def test_duplicate_owns_path(self):
        self.refuse_text(task("a", owns=["x.py", "x.py"]), "repeats an owns path")

    def test_missing_required_task_field(self):
        text = '  - id: "a"\n    title: "T"\n    goal: "G"\n    deps: []\n'
        self.refuse([str(self.fixture("missing.yaml", HEAD + "tasks:\n" + text))],
                    "missing field 'acceptance'")


class TestExplicitGroups(Harness):
    def explicit(self, groups, body=None):
        head = ('project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "explicit"\n'
                f'wave_groups: {groups}\n')
        body = body if body is not None else task("core") + task("cli", ["core"]) + task("docs")
        return str(self.fixture("explicit.yaml", head + "tasks:\n" + body))

    def test_groups_preserved_in_output(self):
        out = self.ok([self.explicit('[["core", "docs"], ["cli"]]')])
        self.assertEqual(out, "WAVE 1\ncore READY\ndocs READY\nWAVE 2\ncli BLOCKED(core)\n")

    def test_missing_wave_groups(self):
        head = 'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "explicit"\n'
        path = self.fixture("nogroups.yaml", head + "tasks:\n" + task("a"))
        self.refuse([str(path)], "wave_groups")

    def test_auto_forbids_wave_groups(self):
        head = 'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "auto"\nwave_groups: [["a"]]\n'
        path = self.fixture("autogroups.yaml", head + "tasks:\n" + task("a"))
        self.refuse([str(path)], "must not supply wave_groups")

    def test_task_missing_from_groups(self):
        self.refuse([self.explicit('[["core"], ["cli"]]')], "missing from wave_groups")

    def test_task_in_two_groups(self):
        self.refuse([self.explicit('[["core", "docs"], ["cli", "docs"]]')],
                    "appears in groups 1 and 2")

    def test_dependency_not_in_earlier_group(self):
        self.refuse([self.explicit('[["docs", "cli"], ["core"]]')],
                    "needs 'core' in an earlier group")

    def test_empty_group(self):
        self.refuse([self.explicit('[["core", "docs", "cli"], []]')],
                    "non-empty list of identifiers")

    def test_unknown_task_in_group(self):
        self.refuse([self.explicit('[["core", "docs", "cli", "ghost"]]')],
                    "task 'ghost'")

    def test_groups_do_not_block_ready_tasks(self):
        path = self.explicit('[["core", "docs"], ["cli"]]')
        out = self.ok([path, "--merged", "core", "--running", "cli"])
        self.assertEqual(out, "WAVE 1\ncore MERGED\ndocs READY\nWAVE 2\ncli RUNNING\n")


class TestDagLoaderAcceptanceValidation(Harness):
    def test_omitted_acceptance_defaults_to_true_on_json_nodes_and_subtasks(self):
        import json
        doc = {
            "project": "demo",
            "nodes": [
                {"id": "A01", "title": "Base", "deps": []},
                {
                    "id": "B01",
                    "title": "Parent",
                    "deps": ["A01"],
                    "subtasks": [{"id": "B01-1", "title": "Sub 1", "deps": []}],
                },
            ],
        }
        plan = dag_loader.load(json.dumps(doc))
        self.assertEqual(plan["tasks"]["A01"]["acceptance"], "true")
        self.assertEqual(plan["tasks"]["B01-1"]["acceptance"], "true")

    def test_blank_or_non_string_acceptance_refused_across_nodes_subtasks_and_yaml(self):
        import json
        for bad in ("", "   ", None, 123, []):
            with self.subTest(where="node", value=bad):
                node_doc = {"project": "d", "nodes": [{"id": "A01", "title": "T", "deps": [], "acceptance": bad}]}
                with self.assertRaises(dag_loader.Invalid):
                    dag_loader.load(json.dumps(node_doc))
            with self.subTest(where="subtask", value=bad):
                sub_doc = {
                    "project": "d",
                    "nodes": [{
                        "id": "A01",
                        "title": "T",
                        "deps": [],
                        "acceptance": "pytest",
                        "subtasks": [{"id": "A01-1", "title": "S", "deps": [], "acceptance": bad}],
                    }],
                }
                with self.assertRaises(dag_loader.Invalid):
                    dag_loader.load(json.dumps(sub_doc))
        for bad_yaml_acc in ('""', '"   "'):
            with self.subTest(where="yaml", value=bad_yaml_acc):
                bad_yaml = file_text(task("a").replace('"python3 -m unittest"', bad_yaml_acc))
                with self.assertRaises(dag_loader.Invalid):
                    dag_loader.load(bad_yaml)
        with self.assertRaises(dag_loader.Invalid):
            dag_loader.validate({
                "project": "t",
                "repo": "./t",
                "mode": "new",
                "waves": "auto",
                "tasks": [{"id": "a", "title": "T", "goal": "G", "deps": [], "acceptance": 99}],
            })


if __name__ == "__main__":
    unittest.main()
