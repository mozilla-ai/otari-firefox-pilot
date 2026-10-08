# Otari Firefox pilot: Firefox → MLPA → Otari, on one machine

The pilot of [Otari](https://github.com/mozilla-ai/otari) as the gateway behind
Firefox's AI features, runnable locally. [MLPA](https://github.com/Firefox-AI/MLPA)
runs with `GATEWAY_BACKEND=otari` in front of a local Otari, which calls real
Vertex, Mistral and Exa. Liner is left out of `otari-config.yml` for now, and
`fakes.py` stands in for the provider failures the checks need (a 429 and a
prompt too long for the model).

```
Firefox (Smart Window) ─┐
check.py (as desktop,   ┼──► MLPA :8080 ──► Otari :8100 ──► Vertex, Mistral, Exa (chat, answers, search)
  Android and iOS)      ┘                    │
                                             ├──► Postgres :55432 (otari, app_attest)
                                             └──► Redis :56379 (rate_limits)
```

## Run it

It needs Docker, on macOS or Linux, and in `.env`: `MISTRAL_API_KEY`,
`EXA_API_KEY`, and `VERTEX_PROJECT`, a GCP project with Vertex AI enabled. Put
the key of a service account with the Vertex AI User role in
`state/vertex-sa.json`, or point `VERTEX_CREDENTIALS` at it.

```bash
./run.sh up      # the backend in Docker, then Firefox with Smart Window pointed at it
./run.sh check   # 35 end-to-end checks through MLPA (Liner's and Mistral on Vertex fail for now)
./run.sh down    # stops the backend and drops the databases
```

The Otari dashboard is at http://127.0.0.1:8100 with master key
`sk-otari-pilot`. `docker compose logs -f` follows the backend.

## Where each piece comes from

| Piece | Source | Changes against upstream | Override |
|---|---|---|---|
| Otari | [mozilla-ai/otari@main](https://github.com/mozilla-ai/otari/tree/main) | none, everything is on `main` | `OTARI_SRC` |
| MLPA | [daavoo/MLPA@otari-pilot](https://github.com/daavoo/MLPA/tree/otari-pilot) | [compare with Firefox-AI/MLPA@main](https://github.com/Firefox-AI/MLPA/compare/main...daavoo:MLPA:otari-pilot) | `MLPA_SRC` |
| Firefox | the latest [Nightly](https://www.mozilla.org/firefox/channel/desktop/#nightly), unmodified | none needed; see [Firefox](#firefox) | `FIREFOX` |

## What `up` does

The backend is `docker-compose.yml`: Postgres, Redis, `fakes.py`, Otari and
MLPA, published on the ports above so Firefox can run on the host.
`otari-config.yml` configures Otari and `mlpa.compose.env` configures MLPA.
`mlpa-init` runs once per `up`, before MLPA starts. It creates MLPA's database,
runs its migrations, sets its signup cap and provisions it in Otari. If it
fails, MLPA never starts, and `docker compose logs mlpa-init` says why.

Provisioning uses MLPA's own script (`scripts/otari_provision.py`), the only
writer. For each service type it creates an Otari budget under MLPA's own
budget id (`PUT /api/v1/budgets/{id}`), with MLPA's dollar cap and its
per-user RPM and TPM. It also creates one service key, owned by the user
`mlpa`, that lists all of those budgets. Each request names its service type's
budget in `Otari-End-User-Budget`. MLPA itself only reads at startup.

`./run.sh check` runs `check.py` in MLPA's image, because it mints MLPA access
tokens with MLPA's own code.

## Firefox

`up` starts Firefox on a fresh profile with `firefox/user.js`, which points
Smart Window's chat and search endpoints at the local MLPA. The profile is
already signed in to a fake Mozilla account, so there is nothing to sign up
for. It holds a cached Smart Window token (`signedInUser.json`), and
`firefox/fake-account.js` points Firefox's account servers at a closed port.
MLPA runs with `devauth/sitecustomize.py`, which accepts that one token as that
account and checks every other token against Mozilla accounts as usual. MLPA's
own code is unchanged. To sign in to a real account instead, run `up` with
`REAL_ACCOUNT=1`; MLPA then verifies tokens against production accounts.

All of this is prefs, so any Firefox with Smart Window works without a patch,
with one exception. Smart Window takes its **answers** search path (Exa answers
with citations) only when the endpoint is prod MLPA, whose URL is hard-coded in
`openAIEngine.sys.mjs`. On the local MLPA it searches through its fast path
instead (`/v1/search`, Otari's `exa-search`), which works. `check.py` sends the
answers request (`sw-answer`) the way Firefox does, so the backend side is
covered either way. To use the answers path from Firefox itself, run a build of
[daavoo/firefox@smartwindow-mlpa-pilot-endpoint](https://github.com/mozilla-firefox/firefox/compare/main...daavoo:firefox:smartwindow-mlpa-pilot-endpoint)
with `FIREFOX`. That branch adds `browser.smartwindow.endpoint.isMLPA`, which
`user.js` already sets and stock Firefox ignores.

Android and iOS reach MLPA through their debug endpoint settings, which offer
only dev, stage and prod. They need a deployed MLPA, so `check.py` stands in
for them here. It sends their service types, models and streaming modes, and
authenticates with an MLPA access token (`use-play-integrity`).

## What the checks cover

- **Clients.** Smart Window chat (streamed and not), memories, search and
  answers; iOS summarize and Quick Answers via both Exa and Liner; Android
  Shake to Summarize; the eval harness's `vertex_ai/...` model name.
- **Citations.** Exa and Liner citations arrive under
  `message.provider_specific_fields.citations`.
- **Billing.** Spend lands on the end user, under its service type's budget,
  for search as well as chat. MLPA's `metadata` never reaches a provider.
- **Request tags.** MLPA's `purpose` and `country_code` land on each usage row
  as tags, queryable with `tag=purpose:chat` and summed with
  `group_by_tag=purpose`, the report MLPA builds from LiteLLM's spend logs today.
- **Refusal codes.** Per-user budget gives `{error: 1}`, per-user RPM gives
  `{error: 2}` (each service type separately), a prompt too long for the model
  gives `{error: 3}`, a provider 429 gives `{error: 5}`, and an unknown model
  gives `{error: 8}`. MLPA maps these from Otari's error code, with no text
  matching.
- **Admin API.** MLPA's block, unblock, budget move, user info, list and
  per-service-type counts, all backed by Otari's users API. Moving a user to
  another budget moves its per-minute limits too.

## Not covered yet

- **Real upstreams.**
  - Vertex serves Gemini 3.1 Flash Lite (`global`) and Qwen 3 235B
    (`us-south1`) through Otari's `vertexai` provider (`generateContent`).
    Mistral Small 2503 on Vertex needs enabling in Model Garden, and Otari
    asks for it under the `google` publisher. Pay-as-you-go Vertex answers an
    occasional 429, which reaches clients as `{error: 5}`.
  - Liner's quick-answer API is not OpenAI-compatible (`answer` plus
    `references`). `fakes.py` holds the translation; done properly, it is an
    any-llm provider.
- **Exa answers cost.** Exa bills answers per request, and its answer
  endpoint reports no token usage, so the token-priced `exa_answers:exa` rows
  cost $0. Otari prices per request only for tools such as search (which Exa
  reports its own cost for), not for a chat model.
- **`x-litellm-*` metrics.** MLPA's backend, fallback and cost metrics read
  `unknown` or 0 under Otari.
- **Search response.** Otari returns `date` where Firefox reads
  `publishedDate`. LiteLLM's response has the same gap.
- **Global budget (code 10).** It maps from any non-user budget scope, but no
  scoped ceiling is provisioned on the service keys.
- **Budget admission.** Otari reserves each request's estimated cost up front.
  With `max_tokens: 8192`, a user near their cap is refused sooner than
  LiteLLM, which checks only spend so far.
- **Signup cap.** A user admitted under the cap whose first request then fails
  before reaching Otari keeps the claim until MLPA's next startup reconcile.
