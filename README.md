# allureparser

`allure-history` builds a test history across CI runs from Allure results. It puts tests in rows and runs in columns, then ranks **flaky tests** by how often they flip between pass and fail.

Uses only the Python standard library (3.9+).

## Usage

Give it one `allure-results` directory per CI run. You can also give a parent directory: every directory below it that holds `*-result.json` files counts as one run, at any depth.

```
ci-history/
  build-101/   *-result.json, executor.json
  build-102/allure-results/   (nested layouts from downloaded CI artifacts work too)
  ...
```

```bash
pip install .            # or run in place with: python -m allure_history ...
allure-history --version # check which version is installed
allure-history ci-history/ --html history.html --csv history.csv --json history.json
```

Terminal output:

```
15 runs, 18 tests, 4 flaky
Legend: P passed  F failed  B broken  S skipped  . not run  (lowercase = retried in that run)

flips   rate  retry  history          test
    6    43%      2  pPPFFFpPFPPPFPP  tests.ui.test_checkout.test_pay_with_card
    6    43%      2  PpFFPPFFPpPFPPP  tests.ui.test_search.test_autocomplete
    6    43%      2  PPPPFppFPPPPFFP  tests.api.test_orders.test_create_order
    0     0%      1  ....PPpPPPPPPPP  tests.ui.test_login.test_sso_redirect
```

The HTML report is a single self-contained file. It shows the full matrix with color-coded cells. It renders only the rows in view, so it stays fast for large suites: 3,000 tests × 100 runs loads in about half a second as a 700 KB file. You can:

- filter the tests by name and turn the **flaky only** filter on or off
- sort by flips, rate, or failures
- hover a cell to see where it failed (`tests/test_api.py:42`), the full failure message (including pytest's assertion diff), and the order of retries
- hover a test name to see its distinct failure reasons with counts. One repeated reason usually means one root cause; several different ones point at something environmental

### One folder with results from many runs

If all your results are collected into one `allure-results` folder, point the tool at it:

```bash
allure-history allure-results --html history.html
```

With a single folder, the tool works **per test execution**. Every result file is one execution of one test. Each test's executions are put in time order, wherever they came from, and flips are counted along that sequence, so run boundaries don't matter. A retry is just another execution, so failing and then passing on rerun counts as a flip. In the HTML, each row is one test's timeline, with its latest execution in the rightmost column. Hovering a cell shows when that execution ran.

- `--last N` keeps each test's N most recent executions. `--min-runs N` hides tests with fewer than N executions.
- `--per-run` treats the folder as a single run instead. `--per-execution` pools several folders or snapshots into per-test timelines.
- Snapshots keep every execution, so `allure-history snapshot allure-results -o history/` also works for a folder like this.

**Seeing individual runs.** The folder doesn't record where one run ends and the next begins, so the tool works it out. `allure-pytest` records the machine and the pytest process ID on every result, and each pytest run is its own process. The tool groups results that way, and treats the workers of a parallel (`pytest -n`) run as one run. If a process ID gets reused (common in containers), the tool still splits the runs, because the same test shows up again.

- The terminal lists the 15 most recent runs, with start time, duration, and pass/fail/broken/skipped/retried counts. Use `--runs N` to show a different number, or `--runs 0` for all.
- The HTML report has a **Runs** section, newest first. Click a run to see which tests failed, where, and why, plus which tests passed only after a retry. Hovering a cell in the matrix shows which run that execution belonged to.
- `--json` output includes `runs_detected` with the same information.
- Results without that machine and process information are grouped by the folder or snapshot they came from.

### Options

| Flag | Meaning |
| --- | --- |
| `--html / --csv / --json FILE` | Write the matrix in that format |
| `--last N` | Use only the N most recent runs (per-execution: each test's N most recent executions) |
| `--min-runs N` | Ignore tests that appear in fewer than N runs |
| `--top N` | Number of flaky tests to print (0 = all) |
| `--all` | Print every test in the terminal table, not just flaky ones |
| `--runs N` | Per-execution mode: how many of the most recent runs to list in the terminal (default 15, 0 = all) |
| `--per-execution` / `--per-run` | Order each test's executions by time, or treat each folder/snapshot as one run. Default: per-execution for a single folder, per-run for several |
| `--title TEXT` | Title of the HTML report |
| `--fail-on-flaky` | Exit with code 2 if any flaky test is found (for CI gates). A test that starts failing and keeps failing (`PPPFFF`) has one flip, so it counts too |

Paths can be `allure-results` folders, snapshot files, or folders containing either. See [Keeping history small](#keeping-history-small).

To try it on generated sample data:

```bash
python examples/make_sample.py sample-results
python -m allure_history sample-results --html history.html
```

## Keeping history small

Raw `allure-results` folders are large because of stack traces, steps, and especially attachments. The history needs none of that. After each CI run, save a **snapshot**, then delete the raw results. A snapshot is gzipped JSON with each test's ID, name, status, and timings, plus the failure message (capped at 20 lines / 2,000 characters) and the failure location (the `file.py:LINE` pytest prints at the end of a traceback). It leaves out the rest of the stack trace, steps, labels, and attachments.

```bash
allure-history snapshot allure-results -o history/ \
    --order "$BUILD_NUMBER" --label "#$BUILD_NUMBER" --url "$BUILD_URL" \
    --keep 200            # optional: delete all but the 200 newest snapshots

allure-history history/ --html history.html
```

Measured on a real `allure-pytest` run of 3,000 tests: the raw results hold 1.7 MB of data but use about 12 MB of disk, because each test is its own small file, and that's before any screenshots or logs. The snapshot is about 93 KB, so 100 runs come to roughly 9 MB. Reports also build faster from snapshots, since there are far fewer files to read.

A report built from snapshots matches one built from the raw results, as long as the runs have the same labels (see `--label` below).

- `--order`, `--label` and `--url` override `executor.json` (`buildOrder`, `buildName`, `buildUrl`), so you don't have to write that file in CI. With neither a label nor a `buildName`, the column label is `#<order>`, or the snapshot name if there's no order.
- The file is named `build-<order>.snapshot.json.gz`. Without an order, the name comes from the build name, then the start time. Use `--name` to choose one yourself (letters, digits, `.`, `_`, `-`).
- An existing snapshot is never overwritten unless you pass `--force`.
- `--keep N` deletes only `*.snapshot.json.gz` files in the output folder. Other files there are left alone. It never deletes the snapshot it just wrote; if that snapshot sorts as older than the N newest (e.g. the build counter was reset), it prints a warning.
- Snapshots and raw results folders can be mixed in one report, e.g. `allure-history history/ allure-results/`. If a run is passed both ways (its snapshot and its raw folder), it's counted once. Masked parameter values are never written to a snapshot.

The `history/` folder has to persist between CI runs. Common places to keep it: a CI cache, a storage bucket (S3 or GCS) synced at the start and end of the job, or a dedicated git branch. Each run adds one small file, so any of these works.

## How it works

- **Test identity:** Allure's `historyId` (full name plus parameters), the same key Allure uses for its own history. Each parameterized variant gets its own row. If `historyId` is missing, the tool falls back to `fullName` plus a hash of the parameter values, so masked variants stay separate without exposing their values.
- **Parameters:** a parameter marked `masked` in Allure (passwords, tokens) appears as `******`. Parameters marked `hidden` or `excluded` are left out of the name.
- **Run order:** oldest to newest. It uses `buildOrder` from `executor.json` if every run has one. Otherwise it uses the earliest test start time in each run. A run's column label is `buildName` (or the directory name), linked to `buildUrl`.
- **Retries:** several results with the same `historyId` in one run are treated as retries. The one that started last is the status shown in the cell, as in Allure (attempts with no start time count as oldest). A dot in the cell (or a lowercase letter in the terminal) marks a retried run.
- **Flips:** the number of times the outcome changes between consecutive runs that have a result. `failed` and `broken` both count as "fail", so going from failed to broken is not a flip. `skipped` and runs where the test is missing are left out entirely.
- **Flip rate:** flips ÷ (number of pass/fail results − 1). A test that alternates on every run scores 100%.
- **Failure reasons:** failures are grouped by the first line of the message plus the location, and counted across runs. They appear in the test-name tooltip and as `failure_reasons` in the JSON output.
- **Flaky:** at least one flip across runs, *or* at least one run where retries both failed and passed.
- **Ranking:** most flips first, then flip rate, then the number of runs with mixed retries, then the total number of failures.

## Development

```bash
python -m unittest discover -s tests
```

`tests/test_real_allure.py` also runs an end-to-end check that writes results with the real `allure-pytest` plugin and compares the matrix with the expected outcomes. That test is skipped unless its dependencies are installed:

```bash
pip install pytest allure-pytest pytest-rerunfailures
```

Malformed result files (bad JSON, wrong field types, a byte-order mark, invalid Unicode, extremely deep nesting) are skipped or normalized, with a warning. They never stop the report from being built. On a console that can't display some characters (e.g. a Windows runner using cp1252), those characters are replaced instead of crashing the tool.

CI (`.github/workflows/tests.yml`) runs the suite on Python 3.9 and 3.12, including the `allure-pytest` integration test.
