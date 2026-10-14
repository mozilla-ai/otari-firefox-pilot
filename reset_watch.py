"""Watch a real UTC-midnight budget reset, through MLPA.

pilot-2.md: LiteLLM's periodic resets were unreliable enough to need cron jobs to
unfreeze users. Otari's budgets reset on the UTC calendar (`reset_alignment:
calendar_day`); this watches one do it, with nothing else running:

1. at 23:59 UTC, a new user spends its whole `concurrency` budget ($0.05, ten
   $0.005 requests on the fake `per-request` model), and a ceiling added to the
   key for this run ($0.10) is filled by two more users;
2. it checks the next requests are refused, {error: 1} and {error: 10}, and that
   the user's Retry-After is the wait until midnight;
3. just after 00:00 UTC, the same user is served again, and the ceiling's period
   has restarted, with no restart, no job, and nothing done in between.

It sleeps until 23:59 UTC (or, with --now, only checks the refusals, right away).
Leave it running in the background:

    docker compose --profile check run --rm check python /rig/reset_watch.py
"""

import argparse
import datetime
import os
import sys
import time
import uuid

import httpx

from mlpa.core.utils import issue_mlpa_access_token

MLPA = os.environ.get("MLPA_URL", "http://127.0.0.1:8080")
OTARI = os.environ.get("OTARI_URL", "http://127.0.0.1:8100") + "/api/v1"
OTARI_MASTER = {"Authorization": f"Bearer {os.environ['OTARI_MASTER_KEY']}"}
KEY = os.environ["MLPA_CONCURRENCY_KEY"]

client = httpx.Client(timeout=60)
failures: list[str] = []


def log(message: str) -> None:
    print(f"{datetime.datetime.now(datetime.UTC):%H:%M:%S}Z  {message}", flush=True)


def check(name: str, ok: bool, detail: object = "") -> None:
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        failures.append(name)


def ask(user: str) -> httpx.Response:
    return client.post(
        f"{MLPA}/v1/chat/completions",
        json={"model": "per-request", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
        headers={
            "Authorization": f"Bearer {issue_mlpa_access_token(user)}",
            "use-play-integrity": "true",
            "service-type": "ai",
            "purpose": "chat",
            "litellm-virtual-key": KEY,
        },
    )


def error(response: httpx.Response) -> object:
    try:
        return response.json().get("detail", {}).get("error")
    except (ValueError, AttributeError):
        return None


def sleep_until(moment: datetime.datetime) -> None:
    while (left := (moment - datetime.datetime.now(datetime.UTC)).total_seconds()) > 0:
        time.sleep(min(left, 60))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", action="store_true", help="only check the refusals, right away")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    if not args.now:
        log(f"waiting until {midnight - datetime.timedelta(minutes=1):%Y-%m-%d %H:%M}Z")
        sleep_until(midnight - datetime.timedelta(minutes=1))

    # A ceiling on the key for this run, so another run's spend can't pre-fill it.
    key_id = next(
        k["id"]
        for k in client.get(f"{OTARI}/keys", params={"limit": 1000}, headers=OTARI_MASTER).json()
        if k.get("key_name") == "mlpa-concurrency"
    )
    budget_id = f"reset-ceiling-{uuid.uuid4().hex[:8]}"
    client.put(
        f"{OTARI}/budgets/{budget_id}",
        json={"name": budget_id, "max_budget": 0.10, "reset_alignment": "calendar_day"},
        headers=OTARI_MASTER,
    ).raise_for_status()
    ceiling = client.post(
        f"{OTARI}/scoped-budgets",
        json={"scope_type": "api_token", "scope_id": key_id, "budget_id": budget_id, "name": budget_id},
        headers=OTARI_MASTER,
    )
    ceiling.raise_for_status()
    try:
        spender, *pool = (f"reset-{uuid.uuid4().hex[:10]}" for _ in range(3))
        served = sum(ask(spender).status_code == 200 for _ in range(10))
        refused = ask(spender)
        until_midnight = (midnight - datetime.datetime.now(datetime.UTC)).total_seconds()
        check(
            "before midnight: the user's spent budget refuses with {error: 1}, Retry-After until midnight",
            served == 10
            and refused.status_code == 429
            and error(refused) == 1
            and abs(int(refused.headers.get("retry-after", "0")) - until_midnight) < 60,
            (served, refused.status_code, refused.text[:100], refused.headers.get("retry-after")),
        )
        # The spender's ten requests are half the ceiling; two more users fill
        # the rest, and the next request from any of them is refused.
        filled = sum(ask(user).status_code == 200 for user in pool for _ in range(5))
        over = ask(pool[0])
        check(
            "before midnight: the key's spent ceiling refuses with {error: 10}",
            filled == 10 and over.status_code == 500 and error(over) == 10,
            (filled, over.status_code, over.text[:100]),
        )
        if not args.now:
            sleep_until(midnight + datetime.timedelta(seconds=15))
            again = ask(spender)
            check(
                "after midnight: the same user is served again, with nothing done in between",
                again.status_code == 200,
                (again.status_code, again.text[:100]),
            )
            figures = client.get(
                f"{OTARI}/scoped-budgets/{ceiling.json()['id']}", headers=OTARI_MASTER
            ).json()
            check(
                "after midnight: the ceiling's period restarted at 00:00 UTC",
                (figures.get("period_start") or "").startswith(f"{midnight:%Y-%m-%d}T00:00:00")
                and figures.get("current_spend", 1) <= 0.005 + 1e-9,
                {k: figures.get(k) for k in ("period_start", "current_spend")},
            )
    finally:
        client.delete(f"{OTARI}/scoped-budgets/{ceiling.json()['id']}", headers=OTARI_MASTER)
        client.delete(f"{OTARI}/budgets/{budget_id}", headers=OTARI_MASTER)
    log(f"{len(failures)} failed" if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
