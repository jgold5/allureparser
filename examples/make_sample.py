"""Generate fake allure-results directories for trying out allure-history.

    python examples/make_sample.py sample-results
    python -m allure_history sample-results --html history.html
"""

import json
import random
import sys
import uuid
from pathlib import Path

TESTS = {
    # name: probability of failing in any given run
    **{f"tests.api.test_users.test_case_{i}": 0.0 for i in range(12)},
    "tests.api.test_orders.test_create_order": 0.35,
    "tests.api.test_orders.test_cancel_order": 0.15,
    "tests.ui.test_checkout.test_pay_with_card": 0.5,
    "tests.ui.test_login.test_sso_redirect": 0.08,
    "tests.ui.test_search.test_autocomplete": 0.25,
    "tests.api.test_billing.test_invoice_total": 1.0,
}


def main(out: Path, runs: int = 15, seed: int = 7) -> None:
    rng = random.Random(seed)
    t0 = 1_759_000_000_000
    for r in range(runs):
        run_dir = out / f"build-{r + 1:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        start = t0 + r * 6 * 3_600_000
        (run_dir / "executor.json").write_text(json.dumps({
            "name": "CI", "type": "github", "buildOrder": r + 1,
            "buildName": f"#{1000 + r}", "buildUrl": f"https://ci.example.com/runs/{1000 + r}",
        }))
        for name, p_fail in TESTS.items():
            if name.endswith("test_sso_redirect") and r < 4:
                continue  # test added later
            attempts = []
            status = "failed" if rng.random() < p_fail else "passed"
            if status == "failed" and p_fail < 1.0 and rng.random() < 0.3:
                attempts.append("failed")  # failed, then retried and passed
                status = "passed"
            if name.endswith("test_case_3") and r % 5 == 2:
                status = "skipped"
            attempts.append(status)
            for i, st in enumerate(attempts):
                res = {
                    "uuid": str(uuid.uuid4()), "historyId": uuid.uuid5(uuid.NAMESPACE_URL, name).hex,
                    "fullName": name, "name": name.rsplit(".", 1)[-1], "status": st,
                    "start": start + i * 1000, "stop": start + i * 1000 + 500,
                }
                if st == "failed":
                    res["statusDetails"] = {"message": "AssertionError: expected 200, got 503"}
                if "billing" in name:
                    res["status"] = "broken"
                    res["statusDetails"] = {"message": "KeyError: 'currency'"}
                (run_dir / f"{res['uuid']}-result.json").write_text(json.dumps(res))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "sample-results"))
