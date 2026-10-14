"""Budgets and limits under concurrent requests, through MLPA, across Otari replicas.

pilot-2.md's LiteLLM incidents were budgets that drifted under volume and limits
that each pod counted alone. Each test here fires its requests at once, from
threads released together, at fake models with fixed prices, so the right answer
is exact:

- a user's budget: 50 requests at $0.005 against a $0.05 budget serve exactly 10;
- the key's ceiling (MLPA's global budget): 60 requests from 30 users against a
  $0.10 ceiling serve exactly 20, and the rest get {error: 10};
- a user's RPM: 25 requests against memories' 10 a minute serve exactly 10;
- overflow: the primary's 3 a minute hold across replicas, and the rest spill.

Run it against several replicas (REPLICAS=3 ./run.sh up), where Otari's counts
live in Postgres and Redis rather than in any one process:

    docker compose -f docker-compose.yml -f docker-compose.scale.yml \\
      --profile check run --rm check python /rig/concurrency.py

With LB_LOG set (docker-compose.scale.yml sets it), it also reports which replica
served each request, to show the load really was spread.
"""

import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from urllib.parse import quote

import httpx

from mlpa.core.utils import issue_mlpa_access_token

MLPA = os.environ.get("MLPA_URL", "http://127.0.0.1:8080")
OTARI = os.environ.get("OTARI_URL", "http://127.0.0.1:8100") + "/api/v1"
FAKES = os.environ.get("FAKES_URL", "http://127.0.0.1:9100")
OTARI_MASTER = {"Authorization": f"Bearer {os.environ['OTARI_MASTER_KEY']}"}
CONCURRENCY_KEY = os.environ["MLPA_CONCURRENCY_KEY"]
LB_LOG = os.environ.get("LB_LOG")

client = httpx.Client(
    timeout=120, limits=httpx.Limits(max_connections=300, max_keepalive_connections=300)
)
failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        failures.append(name)


def identity() -> str:
    return f"concurrency-{uuid.uuid4().hex[:10]}"


def request(user: str, service_type: str, purpose: str, model: str, key: str | None) -> tuple[int, object]:
    headers = {
        "Authorization": f"Bearer {issue_mlpa_access_token(user)}",
        "use-play-integrity": "true",
        "service-type": service_type,
        "purpose": purpose,
        "User-Agent": "Mozilla/5.0 (Macintosh) Gecko/20100101 Firefox/146.0",
        **({"litellm-virtual-key": key} if key else {}),
    }
    body = {"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5}
    response = client.post(f"{MLPA}/v1/chat/completions", json=body, headers=headers)
    try:
        code = response.json().get("detail", {}).get("error")
    except (ValueError, AttributeError):
        code = None
    return response.status_code, code


def at_once(calls: list[tuple]) -> list[tuple[int, object]]:
    """Run every call in its own thread, released together."""
    results: list[tuple[int, object]] = [(0, None)] * len(calls)
    barrier = threading.Barrier(len(calls))

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = request(*calls[index])
        except httpx.HTTPError as e:
            results[index] = (0, repr(e))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def outcome(results: list[tuple[int, object]]) -> Counter:
    return Counter(f"{status}" + (f"/{code}" if code is not None else "") for status, code in results)


def end_user(external_id: str, key: str) -> dict:
    """The end user as Otari has it, read with the service key that bills it, once its holds settle."""
    user: dict = {}
    for _ in range(40):
        response = client.get(
            f"{OTARI}/end-users/{quote(external_id, safe='')}", headers={"Authorization": f"Bearer {key}"}
        )
        user = response.json() if response.status_code == 200 else {}
        if user and not user.get("reserved"):
            break
        time.sleep(0.25)
    return user


def lb_offset() -> int:
    return os.path.getsize(LB_LOG) if LB_LOG and os.path.exists(LB_LOG) else 0


def replicas_since(offset: int) -> Counter:
    """Chat completions per replica since ``offset`` in the load balancer's log."""
    if not LB_LOG or not os.path.exists(LB_LOG):
        return Counter()
    with open(LB_LOG) as log:
        log.seek(offset)
        return Counter(
            line.split()[0] for line in log if "/api/v1/chat/completions" in line
        )


start = lb_offset()

# 1. A user's budget: $0.05 holds ten $0.005 requests, and not an eleventh,
# however many arrive at once on however many replicas.
user = identity()
results = at_once([(user, "ai", "chat", "per-request", CONCURRENCY_KEY)] * 50)
counts = outcome(results)
spent = end_user(f"{user}:ai", CONCURRENCY_KEY)
check(
    "user budget: 50 concurrent $0.005 requests on a $0.05 budget -> 10 served, 40 {error: 1}",
    counts == Counter({"200": 10, "429/1": 40}),
    counts,
)
check(
    "user budget: recorded spend is exactly the budget, nothing left held",
    abs(spent.get("spend", -1) - 0.05) < 1e-9 and not spent.get("reserved"),
    {k: spent.get(k) for k in ("spend", "reserved", "budget_id")},
)

# 2. The key's ceiling, MLPA's global budget: added for this test, $0.10 across
# every end user of the key, so 20 of 60 requests from 30 users, each far from
# its own cap.
key_id = next(
    k["id"]
    for k in client.get(f"{OTARI}/keys", params={"limit": 1000}, headers=OTARI_MASTER).json()
    if k.get("key_name") == "mlpa-concurrency"
)
ceiling_budget = f"concurrency-ceiling-{uuid.uuid4().hex[:8]}"
client.put(
    f"{OTARI}/budgets/{ceiling_budget}",
    json={"name": ceiling_budget, "max_budget": 0.10, "reset_alignment": "calendar_day"},
    headers=OTARI_MASTER,
).raise_for_status()
ceiling = client.post(
    f"{OTARI}/scoped-budgets",
    json={"scope_type": "api_token", "scope_id": key_id, "budget_id": ceiling_budget, "name": ceiling_budget},
    headers=OTARI_MASTER,
)
ceiling.raise_for_status()
try:
    users = [identity() for _ in range(30)]
    results = at_once([(u, "ai", "chat", "per-request", CONCURRENCY_KEY) for u in users for _ in range(2)])
    counts = outcome(results)
    figures: dict = {}
    for _ in range(40):
        figures = client.get(f"{OTARI}/scoped-budgets/{ceiling.json()['id']}", headers=OTARI_MASTER).json()
        if not figures.get("reserved_spend"):
            break
        time.sleep(0.25)
    check(
        "global budget: 60 concurrent $0.005 requests under a $0.10 ceiling -> 20 served, 40 {error: 10}",
        counts == Counter({"200": 20, "500/10": 40}),
        counts,
    )
    check(
        "global budget: the ceiling's recorded spend is exactly its cap, nothing left held",
        abs(figures.get("current_spend", -1) - 0.10) < 1e-9 and not figures.get("reserved_spend"),
        {k: figures.get(k) for k in ("current_spend", "reserved_spend", "max_budget")},
    )
finally:
    client.delete(f"{OTARI}/scoped-budgets/{ceiling.json()['id']}", headers=OTARI_MASTER)
    client.delete(f"{OTARI}/budgets/{ceiling_budget}", headers=OTARI_MASTER)

# 3. A user's RPM, counted in Redis: memories allows 10 a minute per user, through
# MLPA's own service key, so 10 of 25, not 10 per replica.
user = identity()
results = at_once([(user, "memories", "memory-generation", "fake-chat", None)] * 25)
counts = outcome(results)
check(
    "user rpm: 25 concurrent requests against memories' 10 a minute -> 10 served, 15 {error: 2}",
    counts == Counter({"200": 10, "429/2": 15}),
    counts,
)

# 4. Overflow: the primary's per-model limit (3 a minute) holds across replicas,
# and every other request spills to the secondary.
before = client.get(f"{FAKES}/counts").json()
results = at_once([(identity(), "ai", "chat", "overflow-probe", None) for _ in range(12)])
after = client.get(f"{FAKES}/counts").json()
primary, secondary = (
    after.get(f"vertexai:{m}", 0) - before.get(f"vertexai:{m}", 0) for m in ("spill-primary", "spill-secondary")
)
check(
    "overflow: 12 concurrent requests, at most 3 on the primary, the rest spilled, all served",
    outcome(results) == Counter({"200": 12}) and primary <= 3 and primary + secondary == 12,
    (outcome(results), primary, secondary),
)

served_by = replicas_since(start)
if served_by:
    print(f"served by replica: {json.dumps(dict(served_by))}")
    check("the requests were spread over more than one replica", len(served_by) > 1, served_by)
else:
    print("one replica (no load balancer log): run with REPLICAS=3 to test across replicas")

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
