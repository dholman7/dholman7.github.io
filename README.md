# dholman7.github.io

Source for [dholman7.github.io](https://dholman7.github.io/), Dan Holman's portfolio: one static page that explains the six projects, how they fit together, and how to run each one, plus [How this was built](https://dholman7.github.io/built.html). The projects themselves are on [github.com/dholman7](https://github.com/dholman7).

## Claims check

Every number on these pages and on the [profile README](https://github.com/dholman7/dholman7) is marked with its source, and [`claims/check.py`](claims/check.py) recomputes it from the repos: test counts by running the suites, rates and costs from agent-eval-gate's published results, counts from the benchmark files. CI runs it on every push and weekly, and fails on any drift.

```sh
uv run claims/check.py --repos ~/src   # folder with the repos cloned side by side
```

Mark a new number as `<span data-claim="id">value</span>` (HTML) or `<!--claim:id-->value<!--/claim-->` (Markdown), and add an extractor for `id`.
