"""Failure reporting for upsert runs.

Three layers, so an unattended/scheduled run can't fail silently:

1. Local audit file — every failed record + its Salesforce error is written to
   output/upsert_failures_<UTC timestamp>.csv for later inspection.
2. Exit code — the caller (fetch_members.main) exits non-zero when any upsert
   failed. A scheduler (GitHub Actions cron, Task Scheduler, etc.) treats a
   non-zero exit as a failed run and surfaces it (e.g. GitHub emails the repo
   owner automatically).
3. Optional webhook — if FAILURE_WEBHOOK_URL is set in the environment, a short
   JSON summary is POSTed there on failure. The payload has a Slack/Teams-style
   `text` field plus structured counts, so it works with an incoming webhook or
   any generic endpoint. Unset -> notifications are simply skipped.
"""
import csv
import os
from datetime import datetime, timezone

import requests

FAILURE_FIELDS = ["Personid", "Firstname", "Lastname", "statusCode", "message"]


def summarize_results(records: list[dict], results: list[dict]) -> tuple[int, list[dict]]:
    """Pair each input record with its Salesforce result (same order) and split
    into a success count and a list of failure detail dicts.
    """
    successes = 0
    failures = []
    for i, rec in enumerate(records):
        # Salesforce returns one result per input record; if the response is
        # somehow short, treat the unmatched trailing records as failures rather
        # than let zip() silently drop them (which would understate the failure
        # count and could let a scheduler see exit 0 despite unprocessed rows).
        res = results[i] if i < len(results) else {
            "success": False,
            "errors": [{"statusCode": "NO_RESULT",
                        "message": "no result returned by Salesforce for this record"}],
        }
        if res.get("success"):
            successes += 1
            continue
        errs = res.get("errors") or []
        failures.append(
            {
                "Personid": rec.get("Personid"),
                "Firstname": rec.get("Firstname"),
                "Lastname": rec.get("Lastname"),
                "statusCode": "; ".join(e.get("statusCode", "") for e in errs),
                "message": "; ".join(e.get("message", "") for e in errs),
            }
        )
    return successes, failures


def write_failure_report(failures: list[dict], out_dir: str = "output") -> str | None:
    """Write failures to a timestamped CSV. Returns the path, or None if there
    were no failures.
    """
    if not failures:
        return None
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(out_dir, f"upsert_failures_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FAILURE_FIELDS)
        writer.writeheader()
        writer.writerows(failures)
    return path


def notify_failures(total: int, successes: int, failures: list[dict], report_path: str | None = None) -> None:
    """POST a failure summary to FAILURE_WEBHOOK_URL if configured; otherwise a
    no-op. Never raises — a broken webhook must not crash the sync (the local
    audit file and exit code still stand).
    """
    url = os.environ.get("FAILURE_WEBHOOK_URL")
    if not url or not failures:
        return
    sample = "; ".join(f"PMI {f['Personid']}: {f['statusCode']}" for f in failures[:5])
    text = (
        f"ThoughtSpot->Salesforce sync: {len(failures)}/{total} upserts FAILED "
        f"({successes} succeeded). Sample: {sample}"
    )
    payload = {
        "text": text,
        "total": total,
        "succeeded": successes,
        "failed": len(failures),
        "report": report_path,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001 - notification must never be fatal
        print(f"(failure webhook POST to FAILURE_WEBHOOK_URL failed: {exc})")


def report_upsert(records: list[dict], results: list[dict]) -> int:
    """Full failure-reporting pass for one upsert batch: summarize, write the
    audit file, notify if configured, print a summary. Returns the failure
    count so the caller can set its exit code.
    """
    successes, failures = summarize_results(records, results)
    if not failures:
        print(f"All {successes} upserts succeeded.")
        return 0

    path = write_failure_report(failures)
    print(
        f"\n{len(failures)} of {len(records)} upserts FAILED "
        f"({successes} succeeded). Details written to {path}"
    )
    # Show a few inline so a person watching the console sees them immediately.
    for f in failures[:5]:
        print(f"  PMI {f['Personid']} ({f['Firstname']} {f['Lastname']}): "
              f"{f['statusCode']} — {f['message']}")
    if len(failures) > 5:
        print(f"  ... and {len(failures) - 5} more in {path}")

    notify_failures(len(records), successes, failures, report_path=path)
    return len(failures)
