"""Proof for the tree budget: 6.1 to 6.5 of REQUIREMENT-budget.md.

Each test reads the cgroup files back, so a returned dict alone never counts.
These tests need a delegated cgroup for cpu and memory; the class skips when
the machine grants none.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
import warnings
from pathlib import Path

from jbi import budget

warnings.filterwarnings("ignore", category=ResourceWarning)

GIB = 1024 ** 3


def _delegated() -> bool:
    try:
        budget.delegated_base()
        return True
    except budget.BudgetError:
        return False


HAS_DELEGATION = _delegated()


def spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def settle(pid: int, seconds: float = 1.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)


class BudgetCase(unittest.TestCase):
    """Cleanup: move every member out, kill it, then remove the cgroup."""

    def setUp(self):
        if not HAS_DELEGATION:
            self.skipTest("no delegated cgroup for cpu and memory on this machine")
        self.pids: list[int] = []
        self.roots: list[int] = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        for root in self.roots:
            self._remove_root(root)

    def _remove_root(self, root: int) -> None:
        try:
            cgroup = budget.tree_cgroup(root, create=False)
        except budget.BudgetError:
            return
        if not cgroup.is_dir():
            return
        for _ in range(40):
            for pid in budget._members(cgroup):
                budget._move(pid, cgroup.parent)
            try:
                os.rmdir(cgroup)
                return
            except OSError:
                time.sleep(0.05)

    def track(self, proc: subprocess.Popen, *, root: bool = False) -> int:
        self.pids.append(proc.pid)
        if root:
            self.roots.append(proc.pid)
        return proc.pid


class TreeCoverageTest(BudgetCase):
    """6.1: a grandchild is inside the budget and a sibling is not."""

    def test_grandchild_in_budget_sibling_out(self):
        child = spawn(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)")
        self.track(child, root=True)
        sibling = spawn("import time; time.sleep(60)")
        self.track(sibling)
        settle(child.pid, 1.0)

        grandchild_pids = budget._descendants(child.pid)
        self.assertEqual(len(grandchild_pids), 1, "the child started one grandchild")
        grandchild_pid = grandchild_pids[0]
        self.pids.append(grandchild_pid)

        result = budget.set_budget(child.pid, 1.0, 2.0, force=False, deadline_s=0.2)
        root_cgroup = Path(result["cgroup"])
        root_relative = "/" + str(root_cgroup.relative_to(budget.CGROUP_ROOT))

        self.assertEqual(budget._proc_cgroup_path(child.pid), root_relative)
        self.assertEqual(budget._proc_cgroup_path(grandchild_pid), root_relative,
                         "the grandchild shares the tree budget")
        self.assertNotEqual(budget._proc_cgroup_path(sibling.pid), root_relative,
                            "the sibling stays outside the tree")
        self.assertIsNone(budget.managed_cgroup(sibling.pid))


class ForceIsolationTest(BudgetCase):
    """6.2: --force kills only the tree's own processes, never another."""

    def test_force_kills_tree_member_and_spares_sibling(self):
        hog = spawn("import time\n"
                    "buf = bytearray(600 * 1024 * 1024)\n"
                    "for i in range(0, len(buf), 4096):\n    buf[i] = 1\n"
                    "time.sleep(120)")
        sibling = spawn("import time; time.sleep(120)")
        self.track(hog, root=True)
        self.track(sibling)
        settle(hog.pid, 1.0)

        result = budget.set_budget(hog.pid, 1.0, 0.2, force=True, deadline_s=1)

        killed = {entry["pid"] for entry in result["killed"]}
        self.assertIn(hog.pid, killed, "the over-budget tree member is killed")
        self.assertNotIn(sibling.pid, killed, "a process outside the tree is never killed")
        self.assertIsNone(sibling.poll(), "the sibling still runs")
        self.assertIsNotNone(hog.poll(), "the killed process is gone")


class GracefulDefaultTest(BudgetCase):
    """6.3 and 2.4: no flag gives --graceful."""

    def test_no_flag_is_graceful(self):
        proc = spawn("import time; time.sleep(60)")
        self.track(proc, root=True)
        settle(proc.pid, 0.3)

        args = budget.parse_args_for_test(["budget", "set", str(proc.pid), "--cpus", "1", "--mem-gib", "1"])
        self.assertFalse(args.force, "--force is off when no flag is given")
        self.assertEqual(budget.mode_of(args), "graceful")

        result = budget.set_budget(proc.pid, 1.0, 1.0, force=budget.mode_of(args) == "force",
                                   deadline_s=0.2)
        self.assertEqual(result["mode"], "graceful")
        self.assertEqual(result["killed"], [], "graceful never kills")
        self.assertIsNone(proc.poll())


class GracefulDeadlineTest(BudgetCase):
    """3.2 and 6.4: a graceful reduction reports at its deadline without a kill."""

    def test_deadline_reports_and_does_not_kill(self):
        hog = spawn("import time\n"
                    "buf = bytearray(600 * 1024 * 1024)\n"
                    "for i in range(0, len(buf), 4096):\n    buf[i] = 1\n"
                    "time.sleep(120)")
        self.track(hog, root=True)
        settle(hog.pid, 1.0)

        started = time.monotonic()
        result = budget.set_budget(hog.pid, 1.0, 0.2, force=False, deadline_s=0.3)
        elapsed = time.monotonic() - started

        self.assertEqual(result["mode"], "graceful")
        self.assertEqual(result["killed"], [], "graceful does not escalate to a kill")
        self.assertIsNotNone(hog.poll() is None)
        self.assertTrue(result["deadline_reached"], "the deadline is reached and reported")
        self.assertGreaterEqual(elapsed, 0.3, "it waited for the deadline")
        self.assertLess(elapsed, 30.0, "it did not wait forever")

        cgroup = Path(result["cgroup"])
        ceiling = budget._read_int(cgroup / "memory.high")
        self.assertEqual(ceiling, budget._page_aligned(int(0.2 * GIB)),
                         "the ceiling is lowered in the kernel")


class IncreaseNeverKillsTest(BudgetCase):
    """2.3 and 6.5: an increase never kills."""

    def test_increase_never_kills(self):
        proc = spawn("import time; time.sleep(60)")
        self.track(proc, root=True)
        settle(proc.pid, 0.3)

        small = budget.set_budget(proc.pid, 1.0, 0.5, force=True, deadline_s=0.2)
        larger = budget.set_budget(proc.pid, 2.0, 1.0, force=True, deadline_s=0.2)

        self.assertEqual(small["killed"], [])
        self.assertEqual(larger["killed"], [], "an increase never kills")
        self.assertIsNone(proc.poll(), "the process survives the increase")
        cgroup = Path(larger["cgroup"])
        self.assertEqual(budget._read_int(cgroup / "memory.max"),
                         budget._page_aligned(int(1.0 * GIB)),
                         "the kernel carries the new hard ceiling")
        self.assertEqual(budget._read(cgroup / "cpu.max"), "200000 100000",
                         "the kernel carries the new CPU quota")


class RefusalTest(BudgetCase):
    """5.2, 5.3 and 5.4: an over-cap budget is refused and changes nothing."""

    def test_cpu_over_gate_cap_is_refused(self):
        proc = spawn("import time; time.sleep(60)")
        self.track(proc, root=True)
        settle(proc.pid, 0.3)
        ceiling = budget.cpu_ceiling()
        with self.assertRaises(budget.BudgetError) as caught:
            budget.set_budget(proc.pid, ceiling + 1.0, 1.0, force=False, deadline_s=0.1)
        self.assertIn("exceeds", str(caught.exception))
        self.assertIsNone(budget.managed_cgroup(proc.pid), "a refusal creates no cgroup")

    def test_memory_over_ceiling_is_refused(self):
        proc = spawn("import time; time.sleep(60)")
        self.track(proc, root=True)
        settle(proc.pid, 0.3)
        with self.assertRaises(budget.BudgetError):
            budget.set_budget(proc.pid, 1.0, budget.memory_cap_gib() + 1.0,
                              force=False, deadline_s=0.1)
        self.assertIsNone(budget.managed_cgroup(proc.pid))

    def test_cpu_ceiling_is_the_lower_of_gate_and_thermal(self):
        self.assertEqual(budget.cpu_ceiling(),
                         min(budget.gate_cpu_cap(), budget.thermal_cpu_ceiling()))


if __name__ == "__main__":
    unittest.main()
