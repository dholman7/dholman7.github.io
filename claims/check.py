# /// script
# dependencies = ["pyyaml>=6"]
# ///
"""Recomputes every number marked on the portfolio pages from the repos it describes.

A claim is marked where it appears: `<span data-claim="id">value</span>` in HTML (`<tspan>`
inside SVG), and `<!--claim:id-->value<!--/claim-->` in Markdown (GitHub hides the comments). Each id has one
extractor below that derives the value from the repos, cloned side by side under `--repos`.
Fails on a mismatch, an id with no extractor, or an extractor no page uses.

    uv run claims/check.py --repos ~/src/portfolio
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import cache
from pathlib import Path

import yaml

HTML_CLAIM = re.compile(r'<t?span data-claim="([\w.]+)">(.*?)</t?span>')
MD_CLAIM = re.compile(r"<!--claim:([\w.]+)-->(.*?)<!--/claim-->")
WORDS = {n: w for n, w in enumerate("zero one two three four five six seven eight nine ten".split())}

REPOS: Path = Path()


def pct(successes: int, n: int) -> str:
    return f"{round(100 * successes / n)}%"


def run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed in {cwd}:\n{result.stdout[-2000:]}{result.stderr[-2000:]}")
    return result.stdout


# --- agent-eval-gate -------------------------------------------------------------------------


def baseline(name: str) -> dict:
    return json.loads((REPOS / "agent-eval-gate" / "baselines" / f"{name}.json").read_text())


def rate(name: str, metric: str = "task_success") -> dict:
    return baseline(name)["summary"][metric]


def bench_items(path: str, key: str) -> list:
    return yaml.safe_load((REPOS / "agent-eval-gate" / "benchmarks" / path).read_text())[key]


STORYWRIGHT = "storywright-v0.3-v2-heldout-k3"
SINGLE_CALL = "single-call-v2-heldout-k3"
STORYWRIGHT_OPUS = "storywright-v0.2-report-heldout"
API_BENCH = "vendor-intake/v2"
UI_BENCH = "vendor-intake-ui/v1"


def same_model_comparison() -> None:
    """The headline compares Storywright and the single call; it's only fair on the same setup."""
    keys = ("benchmark", "split", "trials", "model")
    a, b = baseline(STORYWRIGHT)["meta"], baseline(SINGLE_CALL)["meta"]
    if any(a[k] != b[k] for k in keys):
        raise RuntimeError(f"headline runs differ: {[(k, a[k], b[k]) for k in keys if a[k] != b[k]]}")


def sw_rate() -> str:
    same_model_comparison()
    r = rate(STORYWRIGHT)
    return pct(r["successes"], r["n"])


def sc_rate() -> str:
    r = rate(SINGLE_CALL)
    return pct(r["successes"], r["n"])


def published_spend() -> float:
    """API cost of every published run: mean cost per trial times trials."""
    total = 0.0
    for path in (REPOS / "agent-eval-gate" / "baselines").glob("*.json"):
        cost = json.loads(path.read_text())["summary"].get("cost_usd") or {}
        total += (cost.get("value") or 0) * (cost.get("n") or 0)
    return total


def tasks_total() -> int:
    return len(bench_items(f"{API_BENCH}/tasks.yaml", "tasks")) + len(
        bench_items(f"{UI_BENCH}/tasks.yaml", "tasks")
    )


def bugs_total() -> int:
    return len(bench_items(f"{API_BENCH}/bugs.yaml", "bugs")) + len(
        bench_items(f"{UI_BENCH}/bugs.yaml", "bugs")
    )


# --- vendor-intake-app -----------------------------------------------------------------------


@cache
def backend_run() -> tuple[int, str]:
    """Passed-test count and coverage, from the same pytest run `make test` does."""
    backend = REPOS / "vendor-intake-app" / "backend"
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        out = run(
            ["uv", "run", "pytest", "--cov=vendor_intake", f"--cov-report=json:{report}"], backend
        )
        covered = json.loads(report.read_text())["totals"]["percent_covered_display"]
    passed = re.search(r"(\d+) passed", out)
    if not passed:
        raise RuntimeError(f"no pass count in pytest output:\n{out[-1000:]}")
    return int(passed.group(1)), f"{covered}%"


def playwright_count(*paths: str) -> str:
    out = run(["npx", "playwright", "test", "--list", *paths], REPOS / "vendor-intake-app" / "e2e")
    total = re.search(r"Total: (\d+) tests?", out)
    if not total:
        raise RuntimeError(f"no total in playwright --list output:\n{out[-1000:]}")
    return total.group(1)


def scenario_count() -> str:
    features = (REPOS / "vendor-intake-app" / "features").glob("*.feature")
    return str(
        sum(len(re.findall(r"^\s*Scenario( Outline)?:", f.read_text(), re.M)) for f in features)
    )


# --- agent-dev-skills (review-loop) ----------------------------------------------------------


def review_rows() -> list[list[str]]:
    """The README's Opus-with-verification rows: bug-catch table first, risky-clean table second.

    The README is the source until the eval results are committed as data.
    """
    readme = (REPOS / "agent-dev-skills" / "README.md").read_text()
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in readme.splitlines()
        if line.startswith("| Claude Opus 5.5, with verification |")
    ]
    if len(rows) < 2:
        raise RuntimeError("review-loop README no longer has both Opus result rows")
    return rows


def fraction(cell: str) -> tuple[int, int]:
    match = re.search(r"(\d+)/(\d+)", cell)
    if not match:
        raise RuntimeError(f"no fraction in {cell!r}")
    return int(match.group(1)), int(match.group(2))


def rl_false_alarms() -> tuple[int, int]:
    bugs, hard = review_rows()[:2]
    easy_fa, easy_n = fraction(bugs[2])
    hard_fa, hard_n = fraction(hard[1])
    return easy_fa + hard_fa, easy_n + hard_n


# --- extractors ------------------------------------------------------------------------------

EXTRACTORS: dict[str, Callable[[], str]] = {
    "sw.v2.rate": sw_rate,
    "sw.v2.frac": lambda: "{successes}/{n}".format(**rate(STORYWRIGHT)),
    "sw.v2.trials": lambda: str(rate(STORYWRIGHT)["n"]),
    "sw.v2.cost": lambda: f"${rate(STORYWRIGHT, 'cost_usd')['value']:.4f}",
    "sc.v2.rate": sc_rate,
    "sw.opus.frac": lambda: "{successes}/{n}".format(**rate(STORYWRIGHT_OPUS)),
    "bench.tasks": lambda: str(tasks_total()),
    "bench.api_tasks": lambda: str(len(bench_items(f"{API_BENCH}/tasks.yaml", "tasks"))),
    "bench.ui_tasks": lambda: str(len(bench_items(f"{UI_BENCH}/tasks.yaml", "tasks"))),
    "bench.bugs": lambda: str(bugs_total()),
    "vi.backend_tests": lambda: str(backend_run()[0]),
    "vi.coverage": lambda: backend_run()[1],
    "vi.playwright_tests": lambda: playwright_count(),
    "vi.generated_tests": lambda: playwright_count("api/generated"),
    "vi.scenarios": scenario_count,
    "rl.opus.caught": lambda: str(fraction(review_rows()[0][1])[0]),
    "rl.bugs": lambda: str(fraction(review_rows()[0][1])[1]),
    "rl.false_alarms": lambda: str(rl_false_alarms()[0]),
    "rl.clean_total": lambda: str(rl_false_alarms()[1]),
    "rl.clean_hard": lambda: str(fraction(review_rows()[1][1])[1]),
    "mcp.tools": lambda: WORDS[
        (REPOS / "ci-context-mcp" / "src" / "ci_context_mcp" / "server.py")
        .read_text()
        .count("@server.tool(")
    ],
    "tel.runs": lambda: str(len(list((REPOS / "agent-eval-gate" / "baselines").glob("*.json")))),
    "spend.published": lambda: f"${published_spend():.2f}",
}


def claims_in(path: Path) -> list[tuple[str, str]]:
    text = path.read_text()
    pattern = HTML_CLAIM if path.suffix == ".html" else MD_CLAIM
    return pattern.findall(text)


def main() -> int:
    global REPOS
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repos", type=Path, required=True, help="folder holding the cloned repos")
    args = parser.parse_args()
    REPOS = args.repos.expanduser().resolve()

    site = Path(__file__).resolve().parent.parent
    pages = [site / "index.html", site / "built.html", REPOS / "dholman7" / "README.md"]
    problems: list[str] = []
    used: set[str] = set()
    actual: dict[str, str] = {}
    for page in pages:
        for claim_id, shown in claims_in(page):
            used.add(claim_id)
            if claim_id not in EXTRACTORS:
                problems.append(f"{page.name}: unknown claim id {claim_id!r}")
                continue
            if claim_id not in actual:
                try:
                    actual[claim_id] = EXTRACTORS[claim_id]()
                except (RuntimeError, OSError, KeyError, subprocess.TimeoutExpired) as error:
                    problems.append(f"{claim_id}: could not compute: {error}")
                    actual[claim_id] = "<error>"
            expected = actual[claim_id]
            mark = "ok  " if shown.lower() == expected.lower() else "FAIL"
            print(f"{mark} {page.name:12} {claim_id:22} shown {shown!r:12} source {expected!r}")
            if mark == "FAIL" and expected != "<error>":
                problems.append(f"{page.name}: {claim_id} shows {shown!r}, source says {expected!r}")
    for unused in sorted(set(EXTRACTORS) - used):
        problems.append(f"extractor {unused!r} is used by no page")

    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(f"{len(actual)} claims checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
