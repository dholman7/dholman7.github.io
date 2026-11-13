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


API_BENCH = "vendor-intake/v2"
UI_BENCH = "vendor-intake-ui/v1"
# The blind v3 held-out runs (agent-eval-gate's README, "Blind benchmark (v3)").
V3 = {
    "storywright": "storywright-v3-heldout-k3",
    "claude-code": "claude-code-v3-heldout-k3",
    "single-call": "single-call-v3-heldout-k3",
    "storywright-opus": "storywright-v3-report-heldout-k3",
}
# Published runs billed to a Claude subscription through the claude-code transports. Their
# costs are list-price estimates, not money spent, so API spend leaves them out.
SUBSCRIPTION_BENCHMARKS = {"vendor-intake@v3"}


def v3(agent: str, metric: str) -> dict:
    """A v3 held-out rate, checked to be on the same benchmark, split, and trials as the rest."""
    keys = ("benchmark", "split", "trials")
    first = baseline(V3["storywright"])["meta"]
    meta = baseline(V3[agent])["meta"]
    if any(meta[k] != first[k] for k in keys):
        raise RuntimeError(f"v3 runs differ: {[(k, meta[k], first[k]) for k in keys if meta[k] != first[k]]}")
    return rate(V3[agent], metric)


def v3_pct(agent: str, metric: str = "bug_detection") -> str:
    r = v3(agent, metric)
    return pct(r["successes"], r["n"])


@cache
def v3_bug_p() -> str:
    """The paired p for bug detection, Claude Code vs Storywright on Haiku, from `aeg gate`."""
    base = f"baselines/{V3['claude-code']}.json"
    current = f"baselines/{V3['storywright']}.json"
    result = subprocess.run(
        ["uv", "run", "aeg", "gate", "--baseline", base, "--current", current],
        cwd=REPOS / "agent-eval-gate", capture_output=True, text=True, timeout=900,
    )  # exits 1 on a regression, which still prints the table
    row = re.search(r"^\| bug detection \|.*\| (\d\.\d+) \|", result.stdout, re.M)
    if not row:
        raise RuntimeError(f"no bug-detection row in aeg gate output:\n{result.stdout[-1000:]}")
    return f"{float(row.group(1)):.2f}"


def review_cost(model: str) -> float:
    """Cost per review from review-loop's API-billed eval runs: total cost over reviews."""
    paths = sorted((REPOS / "agent-dev-skills" / "evals" / "review" / "results").glob(f"{model}-[0-9].json"))
    if not paths:
        raise RuntimeError(f"no review-loop {model} eval runs")
    results = [json.loads(p.read_text()) for p in paths]
    return sum(r["cost_usd"] for r in results) / sum(len(r["rows"]) for r in results)


def heldout_tasks(bench: str) -> str:
    return str(sum(t["split"] == "heldout" for t in bench_items(f"{bench}/tasks.yaml", "tasks")))


def published_spend() -> float:
    """API cost of every published run billed to the API: mean cost per trial times trials."""
    total = 0.0
    for path in (REPOS / "agent-eval-gate" / "baselines").glob("*.json"):
        result = json.loads(path.read_text())
        if result["meta"]["benchmark"] in SUBSCRIPTION_BENCHMARKS:
            continue
        cost = result["summary"].get("cost_usd") or {}
        total += (cost.get("value") or 0) * (cost.get("n") or 0)
    return total


def subscription_runs() -> str:
    paths = (REPOS / "agent-eval-gate" / "baselines").glob("*.json")
    return str(sum(json.loads(p.read_text())["meta"]["benchmark"] in SUBSCRIPTION_BENCHMARKS for p in paths))


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


# --- storywright and the gate's power -------------------------------------------------------


def guard_probes(kind: str) -> str:
    """How many repair-guard probes CI runs: `rejects` (weakenings) or `allows` (real repairs)."""
    out = run(
        # The full test-name prefix: a bare "allows" also matches the probe "swallows it ...".
        ["uv", "run", "pytest", "--collect-only", "-q", "tests/test_guards.py", "-k"]
        + [f"test_the_guard_{kind}"],
        REPOS / "storywright",
    )
    count = re.search(r"test_guards\.py: (\d+)", out)
    if not count:
        raise RuntimeError(f"no probe count in pytest output:\n{out[-1000:]}")
    return count.group(1)


@cache
def gate_power_at_20() -> tuple[str, str]:
    """(paired, overlap) detection rates for a 20-point drop at 20 tasks, 3 trials."""
    out = run(
        ["uv", "run", "aeg", "power", "--trials", "3", "--drops", "0.2"], REPOS / "agent-eval-gate"
    )
    row = re.search(r"\| 20 pts \| (\d+%) / (\d+%) \|", out)
    if not row:
        raise RuntimeError(f"no 20-point row in aeg power output:\n{out[-1000:]}")
    return row.group(1), row.group(2)


@cache
def extraction_baseline() -> dict:
    """vendor-intake-app's published extraction eval: Haiku 5.5, prompt v2, held-out, 3 trials."""
    path = REPOS / "vendor-intake-app" / "backend" / "evals" / "extraction" / "baselines"
    return json.loads((path / "haiku-heldout.json").read_text())


def extraction_documents() -> str:
    truth = REPOS / "vendor-intake-app" / "backend" / "evals" / "extraction" / "data" / "truth.yaml"
    return str(len(yaml.safe_load(truth.read_text())["docs"]))


# --- prompt injection (three red teams) ------------------------------------------------------


def attack_runs(*names: str) -> tuple[int, int]:
    """Attacked trials over all trials, summed over agent-eval-gate attack runs."""
    hits = total = 0
    for name in names:
        path = REPOS / "agent-eval-gate" / "baselines" / "attacks" / f"{name}.json"
        summary = json.loads(path.read_text())["summary"]["attack_success"]
        hits, total = hits + summary["successes"], total + summary["n"]
    return hits, total


def review_attacks(stage: str) -> tuple[int, int]:
    """Seeded bugs hidden by a payload, over all trials, Haiku and Opus, before or after."""
    hits = total = 0
    for model in ("haiku", "opus"):
        path = REPOS / "agent-dev-skills" / "evals" / "review" / "attacks" / f"{stage}-{model}.json"
        rate_ = json.loads(path.read_text())["attack_success"]
        hits, total = hits + rate_["k"], total + rate_["n"]
    return hits, total


def ci_attacks() -> tuple[int, int]:
    """CI-log payloads that worked, over all scored trials, every model and policy."""
    hits = total = 0
    for path in (REPOS / "ci-context-mcp" / "redteam" / "results").glob("*.json"):
        for row in json.loads(path.read_text())["summary"].values():
            hits, total = hits + row["attack_success"]["successes"], total + row["attack_success"]["n"]
    return hits, total


def frac(pair: tuple[int, int]) -> str:
    return f"{pair[0]}/{pair[1]}"


UNDEFENDED = ("storywright-undefended-haiku", "storywright-undefended-opus")
DEFENDED = ("storywright-defended-haiku", "storywright-defended-opus")


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


def judge_kappa() -> str:
    """Cohen's kappa of review-loop's Opus judge vs hand labels, run exactly as the README says
    (its `--exclude` ids are read from that command, so the two can't drift apart)."""
    repo = REPOS / "agent-dev-skills"
    command = re.search(
        r"review-loop agreement (evals/review/labels\.md) (\S+) \\\n\s+--exclude ([^\n`]+)",
        (repo / "README.md").read_text(),
    )
    if not command:
        raise RuntimeError("agent-dev-skills README no longer shows the agreement command")
    labels, pattern, excluded = command.groups()
    results = sorted(str(p.relative_to(repo)) for p in repo.glob(pattern))
    out = run(
        ["uv", "run", "review-loop", "agreement", labels, *results, "--exclude", *excluded.split()],
        repo,
    )
    return f"{json.loads(out)['kappa']:.2f}"


def rl_false_alarms() -> tuple[int, int]:
    bugs, hard = review_rows()[:2]
    easy_fa, easy_n = fraction(bugs[2])
    hard_fa, hard_n = fraction(hard[1])
    return easy_fa + hard_fa, easy_n + hard_n


# --- extractors ------------------------------------------------------------------------------

EXTRACTORS: dict[str, Callable[[], str]] = {
    "v3.tasks": lambda: heldout_tasks("vendor-intake/v3"),
    "v3.sw.bugs": lambda: v3_pct("storywright"),
    "v3.cc.bugs": lambda: v3_pct("claude-code"),
    "v3.sc.bugs": lambda: v3_pct("single-call"),
    "v3.opus.bugs": lambda: v3_pct("storywright-opus"),
    "v3.sw.success": lambda: v3_pct("storywright", "task_success"),
    "v3.sw.cost": lambda: f"${v3('storywright', 'cost_usd')['value']:.4f}",
    "v3.cc.cost": lambda: f"${v3('claude-code', 'cost_usd')['value']:.4f}",
    "v3.bug_p": v3_bug_p,
    "v3.opus.cost": lambda: f"${v3('storywright-opus', 'cost_usd')['value']:.4f}",
    "v3.sw.false_fail": lambda: "{successes}/{n}".format(**v3("storywright", "false_failure")),
    "rl.review_cost.haiku": lambda: f"${review_cost('haiku'):.4f}",
    "rl.review_cost.opus": lambda: f"${review_cost('opus'):.3f}",
    "inj.sw.before": lambda: frac(attack_runs(*UNDEFENDED)),
    "inj.sw.after": lambda: frac(attack_runs(*DEFENDED)),
    "inj.cc": lambda: frac(attack_runs("claude-code")),
    "inj.sc": lambda: frac(attack_runs("single-call-haiku")),
    "inj.rl.before": lambda: frac(review_attacks("before")),
    "inj.rl.after": lambda: frac(review_attacks("after")),
    "inj.ci": lambda: frac(ci_attacks()),
    "ext.calibration": lambda: f"{extraction_baseline()['summary']['calibration_error']:.3f}",
    "spend.subscription_runs": subscription_runs,
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
    "sw.guard.rejects": lambda: guard_probes("rejects"),
    "sw.guard.allows": lambda: guard_probes("allows"),
    "gate.power20": lambda: gate_power_at_20()[0],
    "gate.overlap20": lambda: gate_power_at_20()[1],
    "rl.judge_kappa": judge_kappa,
    "ext.accuracy": lambda: pct(
        extraction_baseline()["summary"]["field_accuracy"]["successes"],
        extraction_baseline()["summary"]["field_accuracy"]["n"],
    ),
    "ext.confident_wrong": lambda: str(
        extraction_baseline()["summary"]["confident_wrong"]["successes"]
    ),
    "ext.documents": extraction_documents,
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
    pages = [site / "index.html", site / "built.html", site / "rollout.html", REPOS / "dholman7" / "README.md"]
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
