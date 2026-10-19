# Otari Firefox pilot

Firefox's AI features on [Otari](https://github.com/mozilla-ai/otari) instead of LiteLLM, on one
machine. [MLPA](https://github.com/Firefox-AI/MLPA) runs with `GATEWAY_BACKEND=otari` and Otari calls
real Vertex, Mistral, Exa and Liner. `fakes.py` stands in for the failures the checks need.

```
Firefox (Smart Window) ─┐
check.py (as desktop,   ┼──► MLPA :8080 ──► Otari :8100 ──► Vertex, Mistral, Exa, Liner
  Android and iOS)      ┘                    ├──► Postgres :55432
                                             └──► Redis :56379
```

## Run it

Needs Docker and, in `.env`: `MISTRAL_API_KEY`, `EXA_API_KEY`, `LINER_API_KEY` and `VERTEX_PROJECT` (a
GCP project with Qwen 3 235B in `us-south1` and Gemini 3.1 Flash Lite enabled). Put a service account
key with the Vertex AI User role in `state/vertex-sa.json`. A missing key only fails that provider's checks.

```bash
./run.sh up      # backend in Docker, then Firefox Nightly with Smart Window pointed at it
./run.sh check   # end-to-end checks through MLPA
./run.sh down    # stop and drop the databases
```

The first Qwen request after Vertex is idle can take a minute, past MLPA's 30 s timeout. If the first
`check` fails Smart Window chat, run it again. Mistral Small on Vertex fails unless enabled in your project.

Otari's dashboard is at http://127.0.0.1:8100 (master key `sk-otari-pilot`).

| Piece | Source | Override |
|---|---|---|
| Otari | [v0.28.0](https://github.com/mozilla-ai/otari/releases/tag/v0.28.0), unmodified | `OTARI_SRC` |
| MLPA | [daavoo/MLPA@otari-pilot](https://github.com/Firefox-AI/MLPA/compare/main...daavoo:MLPA:otari-pilot) | `MLPA_SRC` |
| Firefox | latest Nightly, unmodified | `FIREFOX` |

## How MLPA uses Otari

- **One key** for signed-in users (guests get their own, below). Each request names the user
  (`<identity>:<service type>`, same ids as LiteLLM) and the service type's budget. Otari creates the
  user on first request.
- **Budgets.** One Otari budget per service type (dollars, RPM, TPM), plus a ceiling on the key as the
  global budget. They are declared in [`otari-config.yml`](otari-config.yml) and reset at UTC midnight.
- **Guests.** Guest tokens (#296) are served on a second key, `mlpa-guest`, with the same owner. It
  may assign only the guest budgets, and its ceiling pools every guest apart from signed-in users.
- **Admin.** MLPA blocks, unblocks and moves users, guests included, with the service key. It holds
  no master key.
- **Errors.** MLPA maps Otari's error codes to its own (1 budget, 2 rate limit, 3 context, 5 provider
  429, 8 unknown model, 10 global budget, 11 guest budget). No text matching.
- **Routing.** Fallback on provider 429/5xx and the "Qwen up to N RPM, then Gemini" overflow are config
  (`routing:` and `rate_limits:` in `otari-config.yml`), counted in Redis across replicas.

MLPA's CI runs its Otari tests against a pinned Otari image (`otari-compatibility-tests`). To run them
locally, with no secrets, on the MLPA branch:

```bash
docker compose -f otari_docker_compose.yaml up -d --wait
POSTGRES_CONTAINER_NAME=otari_postgres bash scripts/migrate-app-attest-database-local.sh
export GATEWAY_BACKEND=otari OTARI_API_BASE=http://localhost:8000 \
  OTARI_SERVICE_KEY=tk-YXPI1CW0aDO2jB9R9nmBouDos1jKmqEMidv6iBnSZ8vqhJ2sDlgLbTawowd8BJZz \
  MLPA_ADMIN_KEY=sk-mlpa-admin-ci DB_USERNAME=otari DB_PASSWORD=otari
MLPA_TEST_REQUIRE_REAL_BACKEND=true uv run pytest src/tests/otari_compatibility -v
```

## What the tests show

- `check.py`: every Firefox client's requests (Smart Window, iOS, Android), tool calls, structured
  output, citations, billing per user and service type, every refusal code, and the admin routes.
- `concurrency.py`, with `REPLICAS=3 ./run.sh up`: budgets, the global ceiling, RPM limits and overflow
  hold exactly under simultaneous requests across three Otari replicas.
- `loadtest.py`: MLPA's load test against a fake model. On real Vertex, Qwen's 429s fall back to Gemini.
- `reset_watch.py`: waits for 00:00 UTC and checks budgets reset with nothing run.

```bash
docker compose -f docker-compose.yml -f docker-compose.scale.yml --profile check run --rm check python /rig/concurrency.py
```

These show correctness, not capacity at production traffic.

## Firefox

`up` starts Nightly on a fresh profile already signed in to a fake account, so there is nothing to sign
up for. `REAL_ACCOUNT=1` uses a real one. Smart Window only takes its Exa answers path against prod
MLPA's URL, so locally it uses the search path instead; `check.py` covers answers. Android and iOS need
a deployed MLPA, so `check.py` sends their requests.

## Cutover

`scripts/migrate_litellm_to_otari.py` on the MLPA branch carries what Otari can't learn on its own:
blocked users, users moved to another budget (testers), and signup-cap claims. It is safe to re-run.

1. Deploy Otari with MLPA's budgets, key and models (export LiteLLM's model list from `/model/info`).
2. Run the script with `--dry-run`, then `--apply`.
3. Switch MLPA to `GATEWAY_BACKEND=otari`.
4. Run it again with `--apply` to catch changes made during the switch.

Spend isn't carried, so users start a fresh budget period. Cutting over at 00:00 UTC keeps that to the
weekly budgets. To roll back, switch MLPA back to LiteLLM; blocks made in Otari meanwhile need replaying.
This hasn't been rehearsed against production data.

## Known gaps

- A stream that fails part-way can't switch model; the client gets a coded error.
- Otari holds each request's worst-case cost up front, so users near their cap are refused sooner.
- Search results return `date`, while Firefox reads `publishedDate`. LiteLLM does the same; the fix
  belongs in Firefox.
