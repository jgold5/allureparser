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
- hover a cell to see the failure message and the order of retries

### Options

| Flag | Meaning |
| --- | --- |
| `--html / --csv / --json FILE` | Write the matrix in that format |
| `--last N` | Use only the N most recent runs |
| `--min-runs N` | Ignore tests that appear in fewer than N runs |
| `--top N` | Number of flaky tests to print (0 = all) |
| `--all` | Print every test in the terminal table, not just flaky ones |
| `--fail-on-flaky` | Exit with code 2 if any flaky test is found (for CI gates) |

To try it on generated sample data:

```bash
python examples/make_sample.py sample-results
python -m allure_history sample-results --html history.html
```

## How it works

- **Test identity:** Allure's `historyId` (full name plus parameters), the same key Allure uses for its own history. Each parameterized variant gets its own row. If `historyId` is missing, the tool falls back to `fullName` plus parameters.
- **Parameters:** a parameter marked `masked` in Allure (passwords, tokens) appears as `******`. Parameters marked `hidden` or `excluded` are left out of the name.
- **Run order:** oldest to newest. It uses `buildOrder` from `executor.json` if every run has one. Otherwise it uses the earliest test start time in each run. A run's column label is `buildName` (or the directory name), linked to `buildUrl`.
- **Retries:** several results with the same `historyId` in one run are treated as retries. The one that started last is the status shown in the cell, as in Allure (attempts with no start time count as oldest). A dot in the cell (or a lowercase letter in the terminal) marks a retried run.
- **Flips:** the number of times the outcome changes between consecutive runs that have a result. `failed` and `broken` both count as "fail", so going from failed to broken is not a flip. `skipped` and runs where the test is missing are left out entirely.
- **Flip rate:** flips ÷ (number of pass/fail results − 1). A test that alternates on every run scores 100%.
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

Malformed result files (bad JSON, wrong field types, a byte-order mark) are skipped or normalized, with a warning. They never stop the report from being built.
