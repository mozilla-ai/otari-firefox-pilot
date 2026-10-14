"""End-to-end checks of the pilot: each client's calls through MLPA, served by Otari.

Run in MLPA's image (``./run.sh check``), so MLPA's own token minting
authenticates the way Android's Play Integrity flow does: an MLPA access token
with ``use-play-integrity: true``. The gateway behind MLPA cannot tell which auth
method a request used, so this stands in for desktop's FxA token and iOS's
App Attest assertion too.
"""

import datetime
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
MLPA_ADMIN = {"master_key": f"Bearer {os.environ['MLPA_ADMIN_KEY']}"}

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
# iOS names a user by its App Attest key id, in standard base64: about half of
# them contain "/". Otari keeps the id as it is, the same one LiteLLM had.
attest = f"q8Zr+0b/{uuid.uuid4().hex}="
r = chat(attest, "s2s", "gemini-3.1-flash-lite", max_tokens=5)
attest_user = otari_end_user(f"{attest}:s2s")
check(
    "ios: an App Attest key id with '/' is served, and kept as it is in Otari",
    r.status_code == 200 and attest_user is not None,
    (r.status_code, r.text[:200]),
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

# Firefox's request shapes, as Smart Window sends them (models are real, so a
# tool call depends on the model choosing one; the prompts ask it to).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_user_memories",
            "description": "Get the user's saved memories.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_the_web",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]
SEARCH_ASK = [
    {
        "role": "user",
        "content": "Search the web for the Firefox release date. Use the search_the_web tool.",
    }
]


def firefox(
    model: str, body: dict, service_type: str = "ai", purpose: str = "chat"
) -> httpx.Response:
    return client.post(
        f"{MLPA}/v1/chat/completions",
        json={"model": model, **body},
        headers=headers(identity(), service_type, purpose),
        timeout=120,
    )


def stream_tool_calls(response: httpx.Response) -> tuple[str | None, list[str], object]:
    """The final finish_reason, the names of the streamed tool calls, and any error event."""
    finish, names, error = None, {}, None
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        error = event.get("error") or error
        for choice in event.get("choices") or []:
            finish = choice.get("finish_reason") or finish
            for call in (choice.get("delta") or {}).get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or ""
                names[call.get("index")] = names.get(call.get("index"), "") + name
    return finish, list(names.values()), error


# Chat always streams with tools, and Firefox runs the calls only when the
# stream ends with finish_reason "tool_calls".
for model in ("qwen3-235b-a22b-instruct-2507-maas", "gemini-3.1-flash-lite", "mistral-small-2603"):
    r = firefox(
        model,
        {
            "messages": SEARCH_ASK,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    finish, names, error = stream_tool_calls(r) if r.status_code == 200 else (None, [], r.text)
    check(
        f"firefox: streamed tool call on {model} ends in tool_calls",
        r.status_code == 200
        and finish == "tool_calls"
        and "search_the_web" in names
        and error is None,
        (r.status_code, finish, names, error),
    )

# Gemini 3's second turn, with the tool call rebuilt as Firefox does: id, type
# and function only, so Gemini's thought signature is gone.
r = firefox("gemini-3.1-flash-lite", {"messages": SEARCH_ASK, "tools": TOOLS, "tool_choice": "auto"})
calls = (r.json()["choices"][0]["message"].get("tool_calls") or []) if r.status_code == 200 else []
second = None
if calls:
    call = calls[0]
    rebuilt = {"id": call["id"], "type": "function", "function": call["function"]}
    second = firefox(
        "gemini-3.1-flash-lite",
        {
            "messages": SEARCH_ASK
            + [
                {"role": "assistant", "content": "", "tool_calls": [rebuilt]},
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "content": json.dumps({"results": [{"title": "Firefox", "snippet": "Released Nov 9, 2004"}]}),
                },
            ],
            "tools": TOOLS,
            "tool_choice": "auto",
        },
    )
check(
    "firefox: gemini 3 tool loop's second turn, without the thought signature",
    second is not None
    and second.status_code == 200
    and bool(second.json()["choices"][0]["message"].get("content")),
    (r.status_code, r.text[:200]) if second is None else (second.status_code, second.text[:200]),
)

# Structured output: memories and Smart Form Fill on Gemini, and Monitor on Qwen,
# which sends its schema with an empty tools list.
OBJECT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "fields",
        "strict": False,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": ["browser", "other"]}},
            "required": ["kind"],
        },
    },
}
ARRAY_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "memories",
        "strict": False,
        "schema": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string", "maxLength": 200}},
                "required": ["text"],
            },
        },
    },
}


def json_content(response: httpx.Response) -> object:
    try:
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


r = firefox(
    "gemini-3.1-flash-lite",
    {"messages": [{"role": "user", "content": "Is Firefox a browser?"}], "response_format": OBJECT_SCHEMA},
)
check(
    "firefox: json_schema object with enum and additionalProperties (gemini)",
    r.status_code == 200 and json_content(r) in ({"kind": "browser"}, {"kind": "other"}),
    (r.status_code, r.text[:200]),
)
r = firefox(
    "gemini-3.1-flash-lite",
    {"messages": [{"role": "user", "content": "Return one memory about Firefox."}], "response_format": ARRAY_SCHEMA},
    service_type="memories",
    purpose="memory-generation",
)
memories = json_content(r)
check(
    "firefox: json_schema array root with minItems and maxLength (gemini, memories)",
    r.status_code == 200 and isinstance(memories, list) and len(memories) >= 1,
    (r.status_code, r.text[:200]),
)
r = firefox(
    "qwen3-235b-a22b-instruct-2507-maas",
    {
        "messages": [{"role": "user", "content": "Is Firefox a browser?"}],
        "response_format": OBJECT_SCHEMA,
        "tools": [],
    },
    service_type="agent",
    purpose="monitor",
)
check(
    "firefox: monitor's json_schema with tools: [] (qwen, agent)",
    r.status_code == 200 and isinstance(json_content(r), dict),
    (r.status_code, r.text[:200]),
)

# Message shapes: grounded search ends with an empty assistant message (Mistral
# rejects that unless it is dropped), title generation with a full one, and
# LLM-as-judge telemetry sends only a system message.
r = firefox(
    "mistral-small-2603",
    {"messages": [{"role": "user", "content": "What is Firefox?"}, {"role": "assistant", "content": ""}]},
)
check("firefox: trailing empty assistant message (mistral)", r.status_code == 200, (r.status_code, r.text[:200]))
r = firefox(
    "qwen3-235b-a22b-instruct-2507-maas",
    {
        "messages": [
            {"role": "system", "content": "Write a three-word title."},
            {"role": "user", "content": "What is Firefox?"},
            {"role": "assistant", "content": "Firefox is a web browser."},
        ]
    },
    purpose="title-generation",
)
check("firefox: trailing assistant message (qwen, title generation)", r.status_code == 200, (r.status_code, r.text[:200]))
r = firefox(
    "gemini-3.1-flash-lite",
    {"messages": [{"role": "system", "content": "Rate this answer from 1 to 5: Firefox is a browser."}]},
    service_type="telemetry",
    purpose="chat",
)
check("firefox: system-only request (gemini, telemetry judge)", r.status_code == 200, (r.status_code, r.text[:200]))

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
# A Qwen call that falls back to Gemini also leaves a row for its failed attempt.
chat_rows = [
    row for row in chat_rows if row["status"] == "success" or not row.get("policy_name")
]
check(
    "otari: smart window chat rows carry MLPA's purpose and country tags",
    len(chat_rows) == 3
    and all(row["tags"] == {"purpose": "chat", "country_code": ""} for row in chat_rows),
    chat_rows,
)

# Exa answers report no tokens and are priced per request
answer_user = otari_end_user(f"{ios}:answer")
answer_rows = usage_rows(answer_user["user_id"], 1) if answer_user else []
check(
    "otari: exa answers are billed per request ($0.005)",
    len(answer_rows) == 1 and answer_rows[0]["cost"] == 0.005,
    answer_rows,
)
# Liner's answers too, at $0.003
liner_user = otari_end_user(f"{ios}:liner-answer")
liner_rows = usage_rows(liner_user["user_id"], 1) if liner_user else []
check(
    "otari: liner answers are billed per request ($0.003)",
    len(liner_rows) == 1 and liner_rows[0]["cost"] == 0.003,
    liner_rows,
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

# MLPA's load tests: LiteLLM's mock_response and custom virtual key become
# Otari's `mock` model and its load-test key (loadtest.py drives them at volume).
load_user = identity()
r = client.post(
    f"{MLPA}/v1/chat/completions",
    json={
        "model": "mock",
        "messages": [{"role": "user", "content": "What is Firefox?"}],
        "mock_response": "Firefox is a web browser.",
    },
    headers={**headers(load_user, "ai", "chat"), "litellm-virtual-key": os.environ["MLPA_LOADTEST_KEY"]},
)
load_end_users = client.get(
    f"{OTARI}/users",
    params={"parent_user_id": "mlpa-loadtest", "external_id": f"{load_user}:ai"},
    headers=OTARI_MASTER,
).json()
check(
    "load test: mock model on the load-test key, apart from MLPA's own users",
    r.status_code == 200
    and otari_end_user(f"{load_user}:ai") is None
    and [u["budget_id"] for u in load_end_users] == ["loadtest"]
    and client.get(f"{FAKES}/counts").json().get("mock:mock", 0) > 0,
    (r.status_code, r.text[:200], load_end_users),
)

# Refusals, and the error codes clients map them to
# mochi-dev's budget ($0.006, otari-config.yml) holds one $0.005 request: the
# first is served, and the second is refused until the budget resets.
spender = identity()
first = chat(spender, "mochi-dev", "per-request", purpose="chat")
r = chat(spender, "mochi-dev", "per-request", purpose="chat")
now = datetime.datetime.now(datetime.UTC)
until_midnight = (
    now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1) - now
).total_seconds()
check(
    "per-user budget refusal -> 429 {error: 1}, retry after UTC midnight",
    first.status_code == 200
    and r.status_code == 429
    and error_code(r) == 1
    and abs(int(r.headers.get("retry-after", "0")) - until_midnight) < 60,
    (first.status_code, r.status_code, r.text, r.headers.get("retry-after")),
)
# The global budget: a ceiling on MLPA's service key, pooling every end user.
# Lowered below any request for a moment, then put back.
global_budget = client.get(f"{OTARI}/budgets/mlpa-global", headers=OTARI_MASTER).json()
client.patch(
    f"{OTARI}/budgets/mlpa-global", json={"max_budget": 0.000001}, headers=OTARI_MASTER
)
try:
    r = chat(identity(), "ai", "gemini-3.1-flash-lite", purpose="chat")
finally:
    client.patch(
        f"{OTARI}/budgets/mlpa-global",
        json={"max_budget": global_budget["max_budget"]},
        headers=OTARI_MASTER,
    )
check(
    "global budget (the service key's ceiling) -> 500 {error: 10} with Retry-After",
    r.status_code == 500
    and error_code(r) == 10
    and r.headers.get("retry-after") == "300",
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
# The same 429 behind a routing policy falls back, and the client never sees it.
fallback_user = identity()
r = chat(fallback_user, "ai", "throttled-with-fallback")
fallback_end_user = otari_end_user(f"{fallback_user}:ai")
fallback_rows = usage_rows(fallback_end_user["user_id"], 2) if fallback_end_user else []
check(
    "provider 429 behind a routing policy falls back -> 200",
    r.status_code == 200
    and sorted(row["status"] == "success" for row in fallback_rows) == [False, True],
    (r.status_code, fallback_rows),
)
# Overflow: the primary takes requests until its per-model limit (3 a minute,
# otari-config.yml) is full, then the rest spill to the secondary, all served.
before = client.get(f"{FAKES}/counts").json()
spill_user = identity()
spilled = [chat(spill_user, "ai", "overflow-probe", max_tokens=5) for _ in range(6)]
after = client.get(f"{FAKES}/counts").json()
primary, secondary = (
    after.get(f"vertexai:{m}", 0) - before.get(f"vertexai:{m}", 0)
    for m in ("spill-primary", "spill-secondary")
)
check(
    "overflow: past the primary's rpm, requests spill to the secondary -> 200",
    all(r.status_code == 200 for r in spilled)
    and primary <= 3
    and primary + secondary == 6
    and secondary >= 3,
    ([r.status_code for r in spilled], primary, secondary),
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
r = client.get(f"{MLPA}/user", headers={"master_key": "Bearer sk-default"})
check("admin: LiteLLM's default master key is refused", r.status_code == 401, r.text)
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
