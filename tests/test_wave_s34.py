"""Section 3/4 audit regression tests (row 16977): M1 wave groups and the wave
gate and complexity routing, M4 served preview truth and the drawer/board data,
and the `jbi recover` verb. Python truth is served; clients only render it."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jbi import cli, dag_loader, models, rigor, waves
from jbi.store import Store


def _task(tid: str, deps: list[str] | None = None, **extra) -> dict:
    return {"id": tid, "title": tid, "goal": f"Build {tid}.", "deps": deps or [],
            "acceptance": "true", "owns": [f"{tid}/"], "components": [], **extra}


class ManualWavesTest(unittest.TestCase):
    def test_wave_groups_apply_in_auto_mode(self):
        plan = waves.compile_plan({"waves": "auto",
                                   "wave_groups": [["a"], ["b", "c"], ["d"]],
                                   "tasks": [_task("a"), _task("b", ["a"]), _task("c", ["a"]),
                                             _task("d", ["b", "c"])]})
        self.assertEqual(plan["waves"], [["a"], ["b", "c"], ["d"]])
        self.assertEqual(plan["tasks"]["d"]["wave"], 3)

    def test_unknown_group_id_falls_back_to_levels(self):
        plan = waves.compile_plan({"waves": "auto", "wave_groups": [["a", "ghost"]],
                                   "tasks": [_task("a"), _task("b", ["a"])]})
        self.assertNotEqual(plan["waves"], [["a", "ghost"]])
        self.assertEqual([t["wave"] for t in plan["tasks"].values()], [1, 2])

    def test_repeated_group_id_falls_back_to_levels(self):
        plan = waves.compile_plan({"wave_groups": [["a"], ["a"]],
                                   "tasks": [_task("a"), _task("b", ["a"])]})
        self.assertEqual([t["wave"] for t in plan["tasks"].values()], [1, 2])

    def test_unnamed_tasks_get_a_trailing_wave(self):
        plan = waves.compile_plan({"waves": "auto", "wave_groups": [["a"]],
                                   "tasks": [_task("a"), _task("b", ["a"])]})
        self.assertEqual(plan["waves"], [["a"], ["b"]])

    def test_dag_loader_carries_groups_in_auto_mode(self):
        doc = dag_loader.parse_dag_json(json.dumps({"project": "p", "repo": ".", "waves": "auto",
                                                    "wave_groups": [["a"], ["b"]],
                                                    "nodes": [_task("a"), _task("b", ["a"])]}))
        self.assertEqual(doc["waves"], "auto")
        self.assertEqual(doc["wave_groups"], [["a"], ["b"]])
        plan = dag_loader.load(json.dumps({"project": "p", "repo": ".", "waves": "auto",
                                           "wave_groups": [["a"], ["b"]],
                                           "nodes": [_task("a"), _task("b", ["a"])]}))
        self.assertEqual(plan["waves"], [["a"], ["b"]])


class WaveGateTest(unittest.TestCase):
    def _plan(self):
        return waves.compile_plan([_task("a"), _task("b", ["a"]),
                                   _task("c"), _task("d")])

    def test_gate_holds_a_ready_task_until_its_wave_opens(self):
        plan = self._plan()
        # c sits in wave 2 next to b (levels() may co-group them); mark wave 1 done.
        for task in plan["tasks"].values():
            if task["wave"] == 1:
                task["status"] = "merged"
        plan["tasks"]["b"]["deps"] = ["a"]     # b stays wave >= 2 with a pending peer
        ranked = waves.prioritize_ready(plan, config={"wave_gate": True})
        gated_waves = {plan["tasks"][tid]["wave"] for tid in ranked}
        self.assertTrue(all(w == 1 or all(plan["tasks"][x]["status"] in ("merged", "skipped")
                                          for group in plan["waves"][:w - 1] for x in group)
                            for w in gated_waves))

    def test_no_gate_drains_ready_work_across_waves(self):
        plan = self._plan()
        for task in plan["tasks"].values():
            if task["wave"] == 1:
                task["status"] = "merged"
        ranked = waves.prioritize_ready(plan, config={})
        self.assertTrue(any(plan["tasks"][tid]["wave"] > 1 for tid in ranked))


class ComplexityRoutingTest(unittest.TestCase):
    def test_small_task_runs_fast_rigor_without_an_override(self):
        level, _ = rigor.effective({"complexity": "s"}, {"rigor": "high", "profile": "balanced"})
        self.assertEqual(level, "fast")

    def test_explicit_rigor_outranks_complexity(self):
        level, _ = rigor.effective({"complexity": "S", "override": {"rigor": "xhigh"}},
                                   {"rigor": "high", "profile": "balanced"})
        self.assertEqual(level, "xhigh")

    def test_plain_task_keeps_config_rigor(self):
        level, _ = rigor.effective({}, {"rigor": "high", "profile": "balanced"})
        self.assertEqual(level, "high")

    def test_tier_clamps_small_implement_work(self):
        # balanced already builds at S; the clamp bites on the max profile.
        self.assertEqual(rigor.tier_for("max", "implement", complexity="S"), "S")
        self.assertEqual(rigor.tier_for("max", "implement"), "XL")
        # Escalation still applies above the small floor (TIERS is XL,L,S,XS).
        self.assertEqual(rigor.tier_for("max", "implement", attempts=2, complexity="S"), "L")

    def test_select_names_the_small_model_for_a_small_task(self):
        state = {"config": {"profile": "max"}, "accounts": {}}
        small = models.select(state, "implement", task={"complexity": "S"})
        large = models.select(state, "implement", task={})
        sim = models.catalog()["providers"]["simulator"]["models"]
        self.assertEqual(small.model, sim["S"])
        self.assertEqual(large.model, sim["XL"])


class ServedPreviewTest(unittest.TestCase):
    def test_preview_serves_pipeline_and_routing_from_python(self):
        plan = waves.compile_plan([_task("a"), _task("b", ["a"], complexity="S")])
        summary = waves.describe_preview(plan, {"rigor": "high", "profile": "max"},
                                         accounts={})
        by_id = {t["id"]: t for wave in summary["waves"] for t in wave["tasks"]}
        self.assertEqual([s[0] for s in by_id["a"]["pipeline"]],
                         [s.name for s in rigor.PIPELINES["high"]])
        self.assertEqual(by_id["a"]["routing"]["provider"], "simulator")
        self.assertEqual(by_id["a"]["routing"]["model"],
                         models.catalog()["providers"]["simulator"]["models"]["XL"])
        # The S task serves its small-model truth and its fast pipeline.
        self.assertEqual(by_id["b"]["routing"]["model"],
                         models.catalog()["providers"]["simulator"]["models"]["S"])
        self.assertEqual([s[0] for s in by_id["b"]["pipeline"]],
                         [s.name for s in rigor.PIPELINES["fast"]])


class CooldownLanesTest(unittest.TestCase):
    def test_lanes_lists_only_cooling_entries(self):
        cooldowns = models.ProviderCooldowns()
        cooldowns.mark("agy/gemini-4-neon-high", 30.0)
        cooldowns.mark("claude", 0.0)          # a zero cooldown never registers
        lanes = cooldowns.lanes()
        self.assertIn("agy/gemini-4-neon-high", lanes)
        self.assertNotIn("claude", lanes)
        self.assertGreaterEqual(lanes["agy/gemini-4-neon-high"], 29.0)


class RecoverTest(unittest.TestCase):
    def _run_dir(self, tmp: Path) -> Path:
        run_dir = tmp / "runs" / "r-recover"
        run_dir.mkdir(parents=True)
        store = Store(run_dir / "run.db", initial={
            "run": {"id": "r-recover", "target": str(tmp / "repo")},
            "config": {"ci_service": "ci-conductor"},
            "accounts": {"simulator": {"enabled": True, "status": "ok", "detail": "", "models": []}},
        })
        store.close()
        return run_dir

    def _repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        def git(*argv: str) -> None:
            import subprocess
            subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True,
                           env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@l",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@l",
                                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                "PATH": __import__("os").environ["PATH"], "HOME": str(tmp)})
        git("init", "-b", "main")
        (repo / "f.txt").write_text("one\n")
        git("add", "."); git("commit", "-m", "c1")
        return repo

    def test_recover_reports_and_resyncs_without_serving(self):
        import subprocess
        tmp = Path(tempfile.mkdtemp(prefix="jbi-recover-"))
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+w", str(tmp)], check=False))
        run_dir = self._run_dir(tmp)
        self._repo(tmp)
        from jbi.tracking import Tracking
        tracking = Tracking(run_dir / "fleet.db")
        tracking.ensure_task(task_id="a", title="a", owns=["a/"], branch="jbi/a",
                             worktree=run_dir / "worktrees" / "a", base_commit="b" * 40)
        tracking.record_ci("a", pr_number=7, head_sha="a" * 40, base_commit="b" * 40,
                           service_reply={"type": "queued", "job": "pr-7"}, state="submitted")
        tracking.close()

        # A merged PR plus a CI service answer: confirm_merge returns full evidence.
        evidence = {"job": "pr-7", "pr_number": 7, "head_sha": "a" * 40,
                    "squash_sha": "c" * 40, "green_run": "pr-7",
                    "reply": {"type": "data", "job": "pr-7", "source": "github", "pr_state": "MERGED"}}
        class StubAdapter:
            def __init__(self, *a, **k): pass
            def confirm_merge(self, repo, handover, tracking=None): return evidence
        probes = []
        def stub_probe(name):
            probes.append(name)
            return mock.Mock(status="ok", detail="fine", models=["m-1"])

        args = mock.Mock(spec=["run_dir", "port", "no_tailscale", "open", "no_menu"])
        args.run_dir, args.port = run_dir, 0
        with mock.patch("jbi.providers.probe", side_effect=stub_probe), \
             mock.patch("jbi.ci_adapter.CIAdapter", StubAdapter):
            rc = cli.recover_command(args, then_resume=False)
        self.assertEqual(rc, 0)
        self.assertEqual(probes, ["simulator"])
        tracking = Tracking(run_dir / "fleet.db")
        try:
            rows = tracking.fleet()["ci_submissions"]
        finally:
            tracking.close()
        green = [row for row in rows if row["state"] == "green" and row["squash_sha"] == "c" * 40]
        self.assertTrue(green, f"no green row carries the squash: {rows}")


if __name__ == "__main__":
    unittest.main()
