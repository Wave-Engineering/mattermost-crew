"""`scripts/ci/gate.sh` (#213): the `ci-gate` job's decision.

`ci-gate` is the one check branch protection requires on `main`, so a gate that passes when it
shouldn't lets a red change merge, and one that fails when it shouldn't blocks every PR. Both
directions are pinned here, along with the workflow wiring that makes the job report on every PR.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "ci" / "gate.sh"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(GATE), *args], capture_output=True, text=True, check=False)


def test_every_job_success_passes() -> None:
    r = gate("unit=success", "install=success", "ci=success")
    assert r.returncode == 0, r.stderr
    assert "unit: success" in r.stdout and "every job passed" in r.stdout


@pytest.mark.parametrize(
    "bad", ["failure", "cancelled", "skipped", "", "timed_out", "SUCCESS", "successful"]
)
def test_any_job_not_exactly_success_fails_and_is_named(bad: str) -> None:
    r = gate("unit=success", f"install={bad}", "ci=success")
    assert r.returncode == 1
    assert "install:" in r.stderr and "FAIL" in r.stderr


def test_one_red_among_many_green_fails() -> None:
    r = gate("unit=success", "install=success", "ci=failure")
    assert r.returncode == 1 and "ci: failure (not success)" in r.stderr


def test_it_reports_every_failure_not_just_the_first() -> None:
    r = gate("unit=failure", "install=cancelled", "ci=success")
    assert r.returncode == 1
    assert "unit: failure" in r.stderr and "install: cancelled" in r.stderr


def test_no_jobs_fails() -> None:
    r = gate()
    assert r.returncode == 1 and "refusing to pass" in r.stderr


@pytest.mark.parametrize("arg", ["unit", "=success", ""])
def test_a_malformed_argument_fails(arg: str) -> None:
    r = gate(arg, "ci=success")
    assert r.returncode == 1 and "malformed" in r.stderr


def _job_ids(text: str) -> list[str]:
    """The ids under `jobs:` (two-space indent), in file order."""
    jobs = text.split("\njobs:\n", 1)[1]
    return re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", jobs, flags=re.MULTILINE)


def _job_block(text: str, job: str) -> str:
    jobs = text.split("\njobs:\n", 1)[1]
    m = re.search(
        rf"^  {re.escape(job)}:\s*$(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        jobs,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, f"no job {job!r} in ci.yml"
    return m.group(1)


def test_the_workflow_wires_the_gate_so_it_reports_on_every_pr() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(text, "gate")
    assert re.search(r"^    name: ci-gate$", block, flags=re.MULTILINE)
    # It must run when a needed job failed or was cancelled, or it would be skipped and the
    # required check would never report (the 2026-04-07 outage).
    assert re.search(r"^    if: always\(\)$", block, flags=re.MULTILINE)
    needs = re.search(r"^    needs: \[([^\]]*)\]$", block, flags=re.MULTILINE)
    assert needs, "the gate must list the jobs it covers"
    covered = {n.strip() for n in needs.group(1).split(",")}
    assert covered == set(_job_ids(text)) - {"gate"}, "the gate must cover every other job"
    for job in covered:
        assert f"needs.{job}.result" in block
    assert "scripts/ci/gate.sh" in block


def test_ci_runs_on_every_pull_request_so_the_gate_always_reports() -> None:
    """A branch or path filter on `pull_request` could leave `ci-gate` unreported, and a required
    check that never reports blocks the PR for good."""
    text = WORKFLOW.read_text(encoding="utf-8")
    trigger = text.split("\npermissions:", 1)[0]
    pr = trigger.split("\n  pull_request:", 1)[1]
    assert not re.search(
        r"^\s+(branches|branches-ignore|paths|paths-ignore|types):", pr, flags=re.MULTILINE
    )
