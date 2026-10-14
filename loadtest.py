"""A load test through MLPA and Otari, shaped like MLPA's own load tests.

MLPA's load tests (MLPA #251, #269) send LiteLLM's ``mock_response`` with a custom
virtual key in ``litellm-virtual-key``. Under Otari the same request carries
Otari's load-test key (``mlpa-loadtest`` in otari-config.yml) and asks for model
``mock``, which fakes.py answers after a fixed ``MOCK_LATENCY_MS``. Everything in
between is real: MLPA's auth, Otari's key, budget and rate-limit checks, the
routing, and the usage rows. ``mock_response`` is sent too, as the existing load
tests do; MLPA drops it under Otari.

    docker compose --profile check run --rm check python /rig/loadtest.py [--rpm 600] [--seconds 30]

Requests go out on a fixed schedule (open loop), so a slow stack shows up as
latency rather than as fewer requests. The report is the client's latency, and
the stack's overhead: that latency minus the fake's.

``--model`` sends a real model instead, through MLPA's own service key, as
Firefox does. With the Qwen name, whose policy spills to Gemini past 100
requests a minute (otari-config.yml), this watches the overflow on real Vertex,
and reports from Otari's usage rows which model served each request:

    ... python /rig/loadtest.py --model qwen3-235b-a22b-instruct-2507-maas --rpm 150 --seconds 60
"""

import argparse
import datetime
import os
from collections import Counter
import statistics
import sys
import threading
import time

import httpx

from mlpa.core.utils import issue_mlpa_access_token

MLPA = os.environ.get("MLPA_URL", "http://127.0.0.1:8080")
OTARI = os.environ.get("OTARI_URL", "http://127.0.0.1:8100") + "/api/v1"
OTARI_MASTER = {"Authorization": f"Bearer {os.environ['OTARI_MASTER_KEY']}"}
LOADTEST_KEY = os.environ["MLPA_LOADTEST_KEY"]
MOCK_LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "50"))


def headers(user: str, token: str, stream: bool, mock: bool = True) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "use-play-integrity": "true",
        "service-type": "ai",
        "purpose": "chat",
        **({"litellm-virtual-key": LOADTEST_KEY} if mock else {}),
        "User-Agent": "Mozilla/5.0 (Macintosh) Gecko/20100101 Firefox/146.0",
        **({"Accept": "text/event-stream"} if stream else {}),
    }


def report_routing(client: httpx.Client, policy: str, started: datetime.datetime) -> None:
    """Which model served this run's requests, from Otari's usage rows under the policy."""
    time.sleep(2)  # Otari writes usage rows in the background.
    rows: list[dict] = []
    while True:
        page = client.get(
            f"{OTARI}/usage",
            params={"start_date": started.isoformat(), "limit": 1000, "skip": len(rows)},
            headers=OTARI_MASTER,
        ).json()
        rows += page
        if len(page) < 1000:
            break
    rows = [row for row in rows if row.get("policy_name") == policy]
    outcome = Counter((row["provider"], row["model"], row["status"]) for row in rows)
    print(f"Otari's usage rows under policy {policy!r}:")
    for (provider, model, status), count in sorted(outcome.items()):
        print(f"  {count:5}  {provider}:{model}  {status}")
    # A request Qwen served is one Qwen success. One Qwen refused with a 429 leaves
    # an `absorbed` Qwen row, then the fallback's row; one that found Qwen full
    # leaves the spill target's row alone, since a full model is not called.
    served = {(p, m): n for (p, m, status), n in outcome.items() if status == "success"}
    absorbed = sum(n for (_, _, status), n in outcome.items() if status == "absorbed")
    primary = next((n for (p, m), n in served.items() if "qwen" in m), 0)
    others = sum(n for (p, m), n in served.items() if "qwen" not in m)
    print(
        f"served by the primary: {primary}; by the next model: {others} "
        f"({absorbed} after a provider 429, {others - absorbed} spilled because the primary was full)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpm", type=int, default=600, help="requests a minute")
    parser.add_argument("--seconds", type=int, default=30, help="how long to send")
    # A fixed pool, so a re-run reuses the same identities rather than filling
    # MLPA's signup cap.
    parser.add_argument("--users", type=int, default=50, help="distinct end users")
    parser.add_argument(
        "--stream-every", type=int, default=2, help="stream one request in N (0: never)"
    )
    parser.add_argument(
        "--model", default="mock", help="a real model or policy instead of the fake (costs real money)"
    )
    args = parser.parse_args()
    mock = args.model == "mock"

    users = [f"loadtest-{i}" if mock else f"loadtest-real-{i}" for i in range(args.users)]
    tokens = {user: issue_mlpa_access_token(user) for user in users}
    client = httpx.Client(
        timeout=60, limits=httpx.Limits(max_connections=200, max_keepalive_connections=200)
    )
    results: list[tuple[int, float, bool]] = []
    lock = threading.Lock()

    def send(index: int) -> None:
        user = users[index % len(users)]
        stream = args.stream_every > 0 and index % args.stream_every == 0
        body = {
            "model": args.model,
            "messages": [{"role": "user", "content": "What is Firefox?"}],
            "stream": stream,
            **({"mock_response": "Firefox is a web browser."} if mock else {"max_tokens": 10}),
        }
        start = time.perf_counter()
        try:
            if stream:
                # Measured to the first content chunk, as a reader sees it.
                with client.stream(
                    "POST", f"{MLPA}/v1/chat/completions", json=body,
                    headers=headers(user, tokens[user], stream, mock),
                ) as response:
                    status = response.status_code
                    lines = response.iter_lines()
                    for line in lines:
                        if line.startswith("data:") and '"content"' in line:
                            break
                    elapsed = time.perf_counter() - start
                    for _ in lines:
                        pass
            else:
                response = client.post(
                    f"{MLPA}/v1/chat/completions", json=body,
                    headers=headers(user, tokens[user], stream, mock),
                )
                status, elapsed = response.status_code, time.perf_counter() - start
        except httpx.HTTPError:
            status, elapsed = 0, time.perf_counter() - start
        with lock:
            results.append((status, elapsed * 1000, stream))

    total = args.rpm * args.seconds // 60
    interval = 60 / args.rpm
    started = datetime.datetime.now(datetime.UTC)
    print(f"{total} requests at {args.rpm}/min over {args.seconds}s, {args.users} users")
    threads: list[threading.Thread] = []
    begin = time.perf_counter()
    for index in range(total):
        delay = begin + index * interval - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        thread = threading.Thread(target=send, args=(index,))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()

    failed = [status for status, _, _ in results if status != 200]
    print(f"served {len(results) - len(failed)}/{len(results)}", end="")
    if failed:
        counts = {status: failed.count(status) for status in sorted(set(failed))}
        print(f", failed by status: {counts}", end="")
    print()
    for label, streamed in (("non-streamed", False), ("streamed (first token)", True)):
        times = sorted(ms for status, ms, s in results if status == 200 and s == streamed)
        if len(times) < 2:
            continue
        p50 = statistics.median(times)
        p95 = times[int(len(times) * 0.95) - 1]
        p99 = times[int(len(times) * 0.99) - 1]
        print(
            f"{label:24} p50 {p50:6.0f} ms  p95 {p95:6.0f} ms  p99 {p99:6.0f} ms"
            + (f"  (overhead p50 {p50 - MOCK_LATENCY_MS:.0f} ms over the fake's {MOCK_LATENCY_MS} ms)" if mock else "")
        )

    if not mock:
        report_routing(client, args.model, started)
        return 1 if failed else 0
    budgets = client.get(f"{OTARI}/budgets", params={"limit": 1000}, headers=OTARI_MASTER).json()
    on_loadtest = next((b["user_count"] for b in budgets if b["budget_id"] == "loadtest"), 0)
    print(f"end users on Otari's loadtest budget: {on_loadtest}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
