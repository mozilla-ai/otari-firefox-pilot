"""End-to-end checks of the pilot: each client's calls through MLPA, served by Otari.

Run in MLPA's image (``./run.sh check``), so MLPA's own token minting
authenticates the way Android's Play Integrity flow does: an MLPA access token
with ``use-play-integrity: true``. The gateway behind MLPA cannot tell which auth
method a request used, so this stands in for desktop's FxA token and iOS's
App Attest assertion too.
"""

import json
import os
import sys
import time
import uuid

import httpx

from mlpa.core.utils import issue_mlpa_access_token

MLPA = os.environ.get("MLPA_URL", "http://127.0.0.1:8080")
OTARI = os.environ.get("OTARI_URL", "http://127.0.0.1:8100") + "/api/v1"
FAKES = os.environ.get("FAKES_URL", "http://127.0.0.1:9100")
OTARI_MASTER = {"Authorization": f"Bearer {os.environ['OTARI_MASTER_KEY']}"}
MLPA_ADMIN = {"master_key": f"Bearer {os.environ['MASTER_KEY']}"}

client = httpx.Client(timeout=60)
failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}"
        + (f"  ({detail})" if not ok and detail else "")
    )
    if not ok:
        failures.append(name)


def identity() -> str:
    return f"pilot-{uuid.uuid4().hex[:10]}"


def headers(user: str, service_type: str, purpose: str | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {issue_mlpa_access_token(user)}",
        "use-play-integrity": "true",
        "service-type": service_type,
        "User-Agent": "Mozilla/5.0 (Macintosh) Gecko/20100101 Firefox/146.0",
    }
    if purpose:
        h["purpose"] = purpose
    return h


def chat(
    user: str,
    service_type: str,
    model: str,
    *,
    stream: bool = False,
    purpose: str | None = None,
    prompt: str = "What is Firefox?",
    max_tokens: int | None = None,
) -> httpx.Response:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return client.post(
        f"{MLPA}/v1/chat/completions",
        json=body,
        headers=headers(user, service_type, purpose),
    )


def error_code(response: httpx.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return detail.get("error") if isinstance(detail, dict) else detail


def citations(response: httpx.Response) -> list:
    message = response.json()["choices"][0]["message"]
    return (message.get("provider_specific_fields") or {}).get("citations") or []


def stream_events(response: httpx.Response) -> list[str]:
    return [
        line[6:] for line in response.text.splitlines() if line.startswith("data: ")
    ]


def otari_end_user(external_id: str) -> dict | None:
    users = client.get(
        f"{OTARI}/users",
        params={"parent_user_id": "mlpa", "external_id": external_id},
        headers=OTARI_MASTER,
    ).json()
    return users[0] if users else None


# One service key for every service type, listing each one's budget
mlpa_keys = [
    k
    for k in client.get(f"{OTARI}/keys", headers=OTARI_MASTER).json()
    if k["user_id"] == "mlpa"
]
check(
    "otari: one service key lists every service type's budget",
    len(mlpa_keys) == 1
    and mlpa_keys[0]["is_service_key"]
    and "end-user-budget-memories-dev" in mlpa_keys[0]["end_user_budget_ids"],
    mlpa_keys,
)


# Health
ready = client.get(f"{MLPA}/health/readiness")
check(
    "readiness: MLPA reports Otari ready",
    ready.status_code == 200 and ready.json()["litellm"].get("gateway") == "otari",
    ready.text,
)

# Desktop Smart Window: chat on ai, streamed with usage, and memories
sw = identity()
r = chat(sw, "ai", "qwen3-235b-a22b-instruct-2507-maas", purpose="chat")
check(
    "smart window: chat (ai)",
    r.status_code == 200 and bool(r.json()["choices"][0]["message"]["content"]),
    r.text,
)
check(
    "smart window: response names the alias the client sent",
    r.status_code == 200 and r.json()["model"] == "qwen3-235b-a22b-instruct-2507-maas",
    r.text,
)
r = chat(sw, "ai", "gemini-3.1-flash-lite", stream=True, purpose="chat")
events = stream_events(r)
usage = [
    json.loads(e)["usage"]
    for e in events
    if e != "[DONE]" and json.loads(e).get("usage")
]
check(
    "smart window: streamed chat ends in [DONE] with usage",
    r.status_code == 200 and events[-1:] == ["[DONE]"] and bool(usage),
    r.text[-300:],
)
r = chat(sw, "memories", "mistral-small-2603", purpose="memory-generation")
check("smart window: memories", r.status_code == 200, r.text)
r = chat(sw, "ai", "vertex_ai/mistral-small-2503", purpose="chat")
check(
    "eval harness model name (vertex_ai/mistral-small-2503)",
    r.status_code == 200,
    r.text,
)

# Smart Window search and its answers path (Exa, citations)
r = client.post(
    f"{MLPA}/v1/search",
    json={"query": "firefox", "max_results": 2},
    headers=headers(sw, "search"),
)
check(
    "smart window: search",
    r.status_code == 200 and len(r.json().get("results", [])) == 2,
    r.text,
)
search_user = otari_end_user(f"{sw}:search")
check(
    "search is billed to an end user on the search budget",
    search_user is not None and search_user["budget_id"] == "end-user-budget-search",
    search_user,
)
r = chat(sw, "sw-answer", "exa")
check(
    "smart window: sw-answer citations in provider_specific_fields",
    r.status_code == 200 and len(citations(r)) >= 1,
    r.text,
)

# iOS: Summarize (s2s), Quick Answers with Exa (answer) and Liner (liner-answer)
ios = identity()
r = chat(ios, "s2s", "gemini-3.1-flash-lite", stream=True)
check(
    "ios: summarize (s2s), streamed",
    r.status_code == 200 and stream_events(r)[-1:] == ["[DONE]"],
    r.text[-300:],
)
r = chat(ios, "answer", "exa")
check(
    "ios: quick answers (exa) citations",
    r.status_code == 200 and len(citations(r)) >= 1,
    r.text,
)
r = chat(ios, "liner-answer", "liner")
check(
    "ios: quick answers (liner) citations",
    r.status_code == 200 and len(citations(r)) >= 1,
    r.text,
)

# Android: Shake to Summarize
android = identity()
r = chat(android, "s2s-android", "moz-summarization", stream=True)
check(
    "android: shake to summarize (moz-summarization), streamed",
    r.status_code == 200 and stream_events(r)[-1:] == ["[DONE]"],
    r.text[-300:],
)

# Spend lands on the right end user, under its service type's budget
ai_user = otari_end_user(f"{sw}:ai")
check(
    "otari: ai end user exists with spend",
    ai_user is not None and ai_user["spend"] > 0,
    ai_user,
)
memories_user = otari_end_user(f"{sw}:memories")
check(
    "otari: one key starts each end user on its service type's budget",
    ai_user is not None
    and ai_user["budget_id"] == "end-user-budget-ai"
    and memories_user is not None
    and memories_user["budget_id"] == "end-user-budget-memories",
    [ai_user, memories_user],
)


def usage_rows(user_id: str, expected: int, **params: str) -> list[dict]:
    """The end user's usage rows, waiting briefly: Otari writes them in the background."""
    rows: list[dict] = []
    for _ in range(20):
        rows = client.get(
            f"{OTARI}/usage",
            params={"user_id": user_id, **params},
            headers=OTARI_MASTER,
        ).json()
        if len(rows) >= expected:
            break
        time.sleep(0.25)
    return rows


# Request tags: MLPA's spend_logs_metadata lands on each usage row
chat_rows = usage_rows(ai_user["user_id"], 3, tag="purpose:chat") if ai_user else []
check(
    "otari: smart window chat rows carry MLPA's purpose and country tags",
    len(chat_rows) == 3
    and all(row["tags"] == {"purpose": "chat", "country_code": ""} for row in chat_rows),
    chat_rows,
)
memory_rows = (
    usage_rows(memories_user["user_id"], 1, tag="purpose:memory-generation")
    if memories_user
    else []
)
check(
    "otari: memories rows are tagged memory-generation",
    len(memory_rows) == 1,
    memory_rows,
)
ios_user = otari_end_user(f"{ios}:s2s")
ios_rows = usage_rows(ios_user["user_id"], 1) if ios_user else []
check(
    "otari: a service type with no purpose is tagged with an empty one",
    len(ios_rows) == 1 and ios_rows[0]["tags"] == {"purpose": "", "country_code": ""},
    ios_rows,
)
summary = (
    client.get(
        f"{OTARI}/usage/summary",
        params={"user_id": ai_user["user_id"], "group_by_tag": "purpose", "dimensions": "none"},
        headers=OTARI_MASTER,
    ).json()
    if ai_user
    else {}
)
by_purpose = {row["key"]: row for row in summary.get("by_tag", [])}
check(
    "otari: spend by purpose sums the end user's chat",
    "chat" in by_purpose
    and by_purpose["chat"]["requests"] == 3
    and by_purpose["chat"]["cost"] > 0,
    summary.get("by_tag"),
)
counts = client.get(f"{FAKES}/counts").json()
check(
    "otari does not forward MLPA's metadata to providers",
    counts.get("metadata_forwarded", 0) == 0,
    counts,
)

# Refusals, and the error codes clients map them to
r = chat(identity(), "telemetry", "gemini-3.1-flash-lite")
check(
    "per-user budget refusal -> 429 {error: 1}",
    r.status_code == 429
    and error_code(r) == 1
    and r.headers.get("retry-after") == "86400",
    (r.status_code, r.text),
)
limited = identity()
# A short reply, so 10 requests stay under the per-user TPM and RPM refuses the 11th.
statuses = [
    chat(limited, "memories", "mistral-small-2603", prompt="Reply with OK.", max_tokens=5)
    for _ in range(11)
]
last = statuses[-1]
check(
    "per-user rpm (memories: 10/min) -> 429 {error: 2} with Retry-After",
    [s.status_code for s in statuses[:10]] == [200] * 10
    and last.status_code == 429
    and error_code(last) == 2
    and "retry-after" in last.headers,
    (last.status_code, last.text),
)
other = chat(limited, "ai", "gemini-3.1-flash-lite")
check(
    "the same identity on another service type has its own limit",
    other.status_code == 200,
    other.text,
)
r = chat(identity(), "ai", "throttled")
check(
    "provider 429 -> 429 {error: 5}",
    r.status_code == 429 and error_code(r) == 5,
    (r.status_code, r.text),
)
r = chat(identity(), "ai", "tiny-context")
check(
    "prompt too long for the model -> 413 {error: 3}",
    r.status_code == 413 and error_code(r) == 3,
    (r.status_code, r.text),
)
r = chat(identity(), "ai", "no-such-model")
check(
    "unknown model -> 400 {error: 8}",
    r.status_code == 400 and error_code(r) == 8,
    (r.status_code, r.text),
)

# MLPA's admin API, now backed by Otari's users API
blocked = identity()
chat(blocked, "ai", "gemini-3.1-flash-lite")
r = client.post(f"{MLPA}/user/{blocked}:ai/block", headers=MLPA_ADMIN)
check("admin: block", r.status_code == 200 and r.json()["blocked"] is True, r.text)
r = chat(blocked, "ai", "gemini-3.1-flash-lite")
check(
    "a blocked user gets 403 {error: 'User is blocked.'}",
    r.status_code == 403 and error_code(r) == "User is blocked.",
    (r.status_code, r.text),
)
client.post(f"{MLPA}/user/{blocked}:ai/unblock", headers=MLPA_ADMIN)
r = chat(blocked, "ai", "gemini-3.1-flash-lite")
check("admin: unblock lets the user back in", r.status_code == 200, r.text)
r = client.post(
    f"{MLPA}/user/{limited}:memories/budget",
    json={"service_type": "memories-dev"},
    headers=MLPA_ADMIN,
)
check(
    "admin: move a user to another budget",
    r.status_code == 200 and r.json()["budget_id"] == "end-user-budget-memories-dev",
    r.text,
)
r = chat(limited, "memories", "mistral-small-2603")
check(
    "the move lifts the user's rpm to the new budget's (memories-dev: 50/min)",
    r.status_code == 200,
    (r.status_code, r.text),
)
r = client.get(f"{MLPA}/user/{sw}:ai")
check(
    "user info: spend from Otari",
    r.status_code == 200 and r.json()["spend"] > 0,
    r.text,
)
r = client.get(
    f"{MLPA}/user/counts-by-service-type",
    headers={"mlpa_ui_access_key": f"Bearer {os.environ['MLPA_UI_ACCESS_KEY']}"},
)
check(
    "admin: users counted per service type",
    r.status_code == 200 and r.json()["service_type_counts"].get("ai", 0) >= 3,
    r.text,
)
r = client.get(f"{MLPA}/user", params={"limit": 5}, headers=MLPA_ADMIN)
check(
    "admin: list users",
    r.status_code == 200 and len(r.json()["users"]) == 5 and r.json()["total"] >= 5,
    r.text,
)

print()
print(f"{len(failures)} failed" if failures else "All checks passed")
sys.exit(1 if failures else 0)
