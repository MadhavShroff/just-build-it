"""One resource budget for a whole process tree.

The budget is one cgroup under the delegated user subtree. Kernel accounting is
the source for every reported number (4.3). A tree is the root process and its
descendants; a cgroup carries the budget to anything they start (1.1, 1.3, 1.5).

    python3 -m jbi budget set <pid> --cpus N --mem-gib N [--graceful|--force] [--deadline-s N]
    python3 -m jbi budget show <pid>

Rule numbers cite REQUIREMENT-budget.md.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")
BUDGET_PREFIX = "jbi-budget."
GIB = 1024 ** 3
CPU_PERIOD = 100_000
DEFAULT_DEADLINE_S = 30.0
# The thermal ceiling is four heavy streams of two workers each (5.3).
THERMAL_LANES = 4
THERMAL_WORKERS = 2
PROTECTED = {1}


class BudgetError(RuntimeError):
    """A refusal. The message names the cause (5.4)."""


# ---- machine limits -------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text().strip()


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return None if text == "max" else int(text)


def gate_cpu_cap() -> float:
    """The live GATE CPU budget: WORKLOAD_CORES x CPU_OVERCOMMIT (5.2)."""
    env = os.environ.get("JBI_BUDGET_GATE_CPUS")
    if env:
        return float(env)
    cores, overcommit = 16.0, 1.0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(part.endswith(b"gate.py") for part in cmdline if part):
                continue
            env_text = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for item in env_text:
            key, _, value = item.partition(b"=")
            if key == b"GATE_WORKLOAD_CORES":
                cores = float(value or cores)
            elif key == b"GATE_CPU_OVERCOMMIT":
                overcommit = float(value or overcommit)
        return cores * overcommit
    return cores * overcommit


def thermal_cpu_ceiling() -> float:
    """The four-lane thermal ceiling. A budget never raises it (5.3)."""
    env = os.environ.get("JBI_BUDGET_THERMAL_CPUS")
    return float(env) if env else float(THERMAL_LANES * THERMAL_WORKERS)


def memory_cap_gib() -> float:
    """The memory ceiling above this tree: the user slice hard limit."""
    env = os.environ.get("JBI_BUDGET_MEM_GIB")
    if env:
        return float(env)
    for candidate in (CGROUP_ROOT / "user.slice/user-1000.slice/user@1000.service",
                      CGROUP_ROOT / "user.slice/user-1000.slice"):
        value = _read_int(candidate / "memory.max")
        if value:
            return value / GIB
    return 1024.0


def cpu_ceiling() -> float:
    return min(gate_cpu_cap(), thermal_cpu_ceiling())


# ---- cgroup location and tree ---------------------------------------------------

def _self_cgroup() -> Path:
    return Path(_proc_cgroup_path(os.getpid()))


def _proc_cgroup_path(pid: int) -> str:
    """The unified path of a pid, such as /user.slice/user-1000.slice/..."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError as error:
        raise BudgetError(f"pid {pid} has no readable cgroup: {error}")
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    raise BudgetError(f"pid {pid} has no unified cgroup line")


def delegated_base() -> Path:
    """The highest writable ancestor of this process that delegates cpu and memory."""
    current = _self_cgroup()
    found: Path | None = None
    while True:
        absolute = CGROUP_ROOT / current.relative_to("/") if current != Path("/") else CGROUP_ROOT
        controllers = ""
        try:
            controllers = _read(absolute / "cgroup.controllers")
        except OSError:
            pass
        if (os.access(absolute, os.W_OK) and "cpu" in controllers.split()
                and "memory" in controllers.split()):
            found = absolute
        if absolute == CGROUP_ROOT:
            break
        current = current.parent
    if found is None:
        raise BudgetError("no delegated cgroup for cpu and memory; cannot set a budget")
    return found


def tree_cgroup(pid: int, *, create: bool) -> Path:
    base = delegated_base()
    path = base / f"{BUDGET_PREFIX}{pid}"
    if create:
        try:
            path.mkdir(exist_ok=True)
        except OSError as error:
            raise BudgetError(f"cannot create cgroup {path}: {error}")
    return path


def managed_cgroup(pid: int) -> Path | None:
    """The managed cgroup that holds a pid, read from the kernel."""
    relative = _proc_cgroup_path(pid)
    for part in Path(relative).parts:
        if part.startswith(BUDGET_PREFIX):
            path = CGROUP_ROOT / relative.lstrip("/")
            return path if path.is_dir() else None
    return None


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Field 4 is ppid; the comm field can hold spaces, so split after the last ')'.
        try:
            fields = stat[stat.rindex(")") + 2:].split()
            ppid = int(fields[1])
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    result: list[int] = []
    queue = [root]
    seen = {root}
    while queue:
        parent = queue.pop()
        for child in children.get(parent, []):
            if child not in seen:
                seen.add(child)
                result.append(child)
                queue.append(child)
    return result


def _move(pid: int, cgroup: Path) -> bool:
    try:
        (cgroup / "cgroup.procs").write_text(str(pid))
        return True
    except OSError:
        return False


def admit_tree(root: int, cgroup: Path) -> None:
    """Move the root and its current descendants into the cgroup (1.1)."""
    if not _move(root, cgroup):
        raise BudgetError(f"cannot move pid {root} into {cgroup}; the tree is not yours to fence")
    # A child spawned between the scan and the move is caught by the next pass.
    for _ in range(3):
        moved = sum(1 for pid in _descendants(root) if _move(pid, cgroup))
        if not moved:
            break


# ---- measurement ----------------------------------------------------------------

def _cpu_cores(cgroup: Path, interval: float = 0.2) -> dict[str, Any]:
    def usage() -> int | None:
        try:
            for line in (cgroup / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
        except (OSError, ValueError):
            return None
        return None

    first = usage()
    if first is None:
        return {"cores_used": None}
    time.sleep(interval)
    second = usage()
    if second is None:
        return {"cores_used": None}
    return {"cores_used": round((second - first) / (interval * 1_000_000), 3)}


def read_use(cgroup: Path) -> dict[str, Any]:
    """The kernel's current account of the tree (4.1, 4.3)."""
    cpu_max_text = _read(cgroup / "cpu.max") if (cgroup / "cpu.max").exists() else "max 100000"
    quota_text, _, _period = cpu_max_text.partition(" ")
    cpu_limit = None if quota_text == "max" else round(int(quota_text) / CPU_PERIOD, 3)
    mem_current = _read_int(cgroup / "memory.current") or 0
    mem_high = _read_int(cgroup / "memory.high")
    mem_max = _read_int(cgroup / "memory.max")
    pids_current = _read_int(cgroup / "pids.current") or 0
    use = {
        "cpu_limit_cores": cpu_limit,
        "cpu_cores_used": _cpu_cores(cgroup)["cores_used"],
        "memory_gib": round(mem_current / GIB, 3),
        "memory_bytes": mem_current,
        "memory_high_gib": None if mem_high is None else round(mem_high / GIB, 3),
        "memory_max_gib": None if mem_max is None else round(mem_max / GIB, 3),
        "procs": pids_current,
    }
    # 4.4: a fraction needs a finite ceiling.
    if mem_max:
        use["memory_fraction"] = round(mem_current / mem_max, 4)
    if cpu_limit:
        use["cpu_fraction"] = round((use["cpu_cores_used"] or 0) / cpu_limit, 4)
    return use


def tree_use(pid: int) -> dict[str, Any]:
    cgroup = managed_cgroup(pid)
    if cgroup is None:
        return {"pid": pid, "managed": False, "cgroup": _proc_cgroup_path(pid)}
    return {"pid": pid, "managed": True, "cgroup": str(cgroup), "use": read_use(cgroup)}


# ---- apply ----------------------------------------------------------------------

def _check_limits(cpus: float, mem_gib: float) -> None:
    if cpus <= 0:
        raise BudgetError("--cpus must be greater than zero")
    if mem_gib <= 0:
        raise BudgetError("--mem-gib must be greater than zero")
    ceiling = cpu_ceiling()
    if cpus > ceiling + 1e-9:
        gate, thermal = gate_cpu_cap(), thermal_cpu_ceiling()
        cause = "the GATE cap" if gate <= thermal else "the thermal four-lane ceiling"
        raise BudgetError(
            f"refused: --cpus {cpus:g} exceeds {cause} of {ceiling:g} cores "
            f"(GATE cap {gate:g}, thermal ceiling {thermal:g}) (5.2, 5.3)")
    memory_ceiling = memory_cap_gib()
    if mem_gib > memory_ceiling + 1e-9:
        raise BudgetError(
            f"refused: --mem-gib {mem_gib:g} exceeds the memory ceiling of "
            f"{memory_ceiling:.1f} GiB (5.2)")


def _write(path: Path, value: str) -> None:
    try:
        path.write_text(value)
    except OSError as error:
        raise BudgetError(f"cannot write {path.name}={value}: {error}")


def _apply_cpu(cgroup: Path, cpus: float) -> None:
    quota = max(1_000, int(round(cpus * CPU_PERIOD)))
    _write(cgroup / "cpu.max", f"{quota} {CPU_PERIOD}")


def _set_memory_hard(cgroup: Path, mem_gib: float) -> None:
    _write(cgroup / "memory.max", str(_page_aligned(int(mem_gib * GIB))))


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return 0
    return 0


def _members(cgroup: Path) -> list[int]:
    try:
        return sorted(int(value) for value in (cgroup / "cgroup.procs").read_text().split())
    except OSError:
        return []


def _verified_members(cgroup: Path) -> list[int]:
    """Candidates from the kernel list, each confirmed against /proc/<pid>/cgroup (6.2)."""
    relative = "/" + str(cgroup.relative_to(CGROUP_ROOT))
    result = []
    for pid in _members(cgroup):
        try:
            if _proc_cgroup_path(pid) == relative:
                result.append(pid)
        except BudgetError:
            continue
    return result


def _kill_to_fit(cgroup: Path, target_bytes: int, root: int) -> tuple[list[dict[str, Any]], bool]:
    """Kill only the cgroup's own processes until memory fits (2.2, 4.2, 6.2)."""
    killed: list[dict[str, Any]] = []
    mine = {os.getpid(), *PROTECTED}
    for _ in range(200):
        current, _source = tree_memory(cgroup)
        if current <= target_bytes:
            break
        victims = [pid for pid in _verified_members(cgroup) if pid not in mine]
        if not victims:
            break
        # Largest first; the root is the last resort so the tree keeps its head.
        victims.sort(key=lambda pid: (pid == root, -_rss(pid)))
        victim = victims[0]
        command = _cmdline(victim)
        try:
            os.kill(victim, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as error:
            raise BudgetError(f"cannot signal pid {victim}: {error}")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not Path(f"/proc/{victim}").exists():
                break
            time.sleep(0.02)
        else:
            try:
                os.kill(victim, signal.SIGKILL)
            except OSError:
                pass
        killed.append({"pid": victim, "cmdline": command, "signal": "SIGTERM"})
    still_over = tree_memory(cgroup)[0] > target_bytes
    return killed, still_over


def _settled_current(cgroup: Path, settle_s: float = 0.5) -> int:
    """memory.current after the move settles; the kernel charges lag one tick."""
    last = _read_int(cgroup / "memory.current") or 0
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = _read_int(cgroup / "memory.current") or 0
        if current == last:
            break
        last = current
    return last


def _rss_sum(cgroup: Path) -> int:
    return sum(_rss(pid) for pid in _verified_members(cgroup))


def tree_memory(cgroup: Path) -> tuple[int, str]:
    """The tree's memory for the kill choice, with its source.

    cgroup v2 charges a page to the cgroup of the task that faults it, so a
    process moved into this cgroup keeps its earlier pages charged elsewhere.
    memory.current is therefore the source; when it reads zero and the members
    hold real pages, the members' VmRSS stands in and the source is named (4.3).
    """
    current = _settled_current(cgroup)
    if current:
        return current, "cgroup"
    resident = _rss_sum(cgroup)
    return resident, ("proc_rss" if resident else "cgroup")


def _wait_graceful(cgroup: Path, target_bytes: int, deadline_s: float) -> bool:
    """Lower the ceiling and let work finish; never kill (2.1, 3.2)."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if tree_memory(cgroup)[0] <= target_bytes:
            return False
        time.sleep(0.05)
    return tree_memory(cgroup)[0] > target_bytes


def _page_aligned(value: int) -> int:
    """The kernel stores these limits in whole pages; align so the read-back matches."""
    return value & ~(os.sysconf("SC_PAGE_SIZE") - 1)


def set_budget(pid: int, cpus: float, mem_gib: float, *, force: bool,
               deadline_s: float = DEFAULT_DEADLINE_S) -> dict[str, Any]:
    """Apply one budget to the whole tree of a pid (1.1 to 1.5, 2.1 to 2.4)."""
    _check_limits(cpus, mem_gib)
    target_bytes = _page_aligned(int(mem_gib * GIB))

    cgroup = tree_cgroup(pid, create=True)
    admit_tree(pid, cgroup)
    before = read_use(cgroup)
    was_over = tree_memory(cgroup)[0] > target_bytes

    _apply_cpu(cgroup, cpus)
    _write(cgroup / "memory.high", str(target_bytes))

    killed: list[dict[str, Any]] = []
    deadline_reached = False
    still_over = False
    if force:
        # 2.3: an increase never kills.
        if was_over:
            killed, still_over = _kill_to_fit(cgroup, target_bytes, pid)
        _set_memory_hard(cgroup, mem_gib)
    else:
        # 2.1 and 3.2: memory.high throttles, so the tree converges without a
        # kill. A hard memory.max at the target would let the kernel OOM-kill,
        # so graceful raises the hard ceiling but never lowers it below the target.
        if (_read_int(cgroup / "memory.max") or 0) < target_bytes:
            _set_memory_hard(cgroup, mem_gib)
        if was_over:
            deadline_reached = _wait_graceful(cgroup, target_bytes, deadline_s)
            still_over = deadline_reached

    after = read_use(cgroup)
    return {
        "action": "set",
        "mode": "force" if force else "graceful",
        "pid": pid,
        "cgroup": str(cgroup),
        "budget": {"cpus": cpus, "mem_gib": mem_gib},
        "before": before,
        "after": after,
        "memory_source": tree_memory(cgroup)[1],
        "killed": killed,
        "deadline_s": None if force else deadline_s,
        "deadline_reached": deadline_reached,
        "still_over": still_over,
    }


# ---- CLI ------------------------------------------------------------------------

def _print(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def mode_of(args) -> str:
    """2.1 and 2.4: no flag gives graceful."""
    return "force" if getattr(args, "force", False) else "graceful"


def parse_args_for_test(argv: list[str]):
    from .cli import parse_args
    return parse_args(argv)


def main(args) -> int:
    try:
        if args.budget_command == "set":
            result = set_budget(args.pid, args.cpus, args.mem_gib,
                                force=mode_of(args) == "force",
                                deadline_s=getattr(args, "deadline_s", DEFAULT_DEADLINE_S))
        elif args.budget_command == "show":
            result = tree_use(args.pid)
        else:
            raise BudgetError("name `set` or `show`")
    except BudgetError as error:
        _print({"error": str(error)})
        return 1
    _print(result)
    return 0
