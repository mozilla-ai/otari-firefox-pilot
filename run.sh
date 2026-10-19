#!/usr/bin/env bash
# The pilot (pilot.md) on one machine: Firefox -> MLPA -> Otari -> fakes.
#
#   ./run.sh up     the backend in Docker (Postgres, Redis, fakes :9100, Otari :8100,
#                   MLPA :8080), then Firefox with Smart Window pointed at MLPA
#   ./run.sh check  end-to-end checks through MLPA, as each client calls it
#   ./run.sh down   stop the backend and drop the databases
#
# `up` builds Otari from its v0.28.0 release and MLPA from the latest commit of its
# otari-pilot branch on GitHub; OTARI_SRC and MLPA_SRC override either with
# another git URL or a local checkout. FIREFOX is the Firefox binary to run
# (default: the latest Nightly for macOS or Linux, downloaded into state/nightly
# on first use; delete that directory to update it).
# REAL_ACCOUNT=1 signs in to a real Mozilla account instead of the fake one the
# profile starts with (see devauth/sitecustomize.py and firefox/fake-account.js).
# REPLICAS=N (N > 1) runs Otari as N replicas behind a load balancer
# (docker-compose.scale.yml); set it for `check` and `down` too.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${HERE}/state"
mkdir -p "${STATE}"

export OTARI_MASTER_KEY="${OTARI_MASTER_KEY:-sk-otari-pilot}"

# The fake Mozilla account: the Smart Window token Firefox holds, and the account
# MLPA resolves it to (the same values as the mlpa service in docker-compose.yml).
REAL_ACCOUNT="${REAL_ACCOUNT:-0}"
PILOT_FXA_TOKEN="pilot-fxa-token"
PILOT_FXA_UID="0123456789abcdef0123456789abcdef"

REPLICAS="${REPLICAS:-1}"
compose() {
  if [ "${REPLICAS}" -gt 1 ]; then
    OTARI_REPLICAS=$((REPLICAS - 1)) docker compose -f "${HERE}/docker-compose.yml" -f "${HERE}/docker-compose.scale.yml" "$@"
  else
    docker compose -f "${HERE}/docker-compose.yml" "$@"
  fi
}

nightly() {
  # The latest Nightly, from Mozilla's download service; prints its binary.
  local url="https://download.mozilla.org/?product=firefox-nightly-latest-ssl&lang=en-US"
  local dir="${STATE}/nightly"
  if [ ! -d "${dir}" ]; then
    local tmp
    tmp="$(mktemp -d)"
    if [ "$(uname)" = Darwin ]; then
      curl -fL --progress-bar -o "${tmp}/nightly.dmg" "${url}&os=osx" >&2
      hdiutil attach -nobrowse -quiet -mountpoint "${tmp}/mnt" "${tmp}/nightly.dmg"
      mkdir -p "${dir}" && cp -R "${tmp}/mnt/Firefox Nightly.app" "${dir}/"
      hdiutil detach -quiet "${tmp}/mnt"
    else
      curl -fL --progress-bar -o "${tmp}/nightly.tar.xz" "${url}&os=linux64" >&2
      mkdir -p "${dir}" && tar -xJf "${tmp}/nightly.tar.xz" -C "${dir}"
    fi
    rm -rf "${tmp}"
  fi
  if [ "$(uname)" = Darwin ]; then echo "${dir}/Firefox Nightly.app/Contents/MacOS/firefox"; else echo "${dir}/firefox/firefox"; fi
}

firefox() {
  local bin profile="${STATE}/firefox-profile"
  bin="${FIREFOX:-$(nightly)}"
  mkdir -p "${profile}"
  cp "${HERE}/firefox/user.js" "${profile}/user.js"
  if [ "${REAL_ACCOUNT}" != "1" ]; then
    cat "${HERE}/firefox/fake-account.js" >>"${profile}/user.js"
    cat >"${profile}/signedInUser.json" <<JSON
{"version": 1, "accountData": {"email": "pilot@example.com", "uid": "${PILOT_FXA_UID}", "verified": true,
 "sessionToken": "0000000000000000000000000000000000000000000000000000000000000000",
 "oauthTokens": {"https://identity.mozilla.com/apps/smartwindow|profile:uid": {"token": "${PILOT_FXA_TOKEN}"}}}}
JSON
  fi
  "${bin}" -no-remote -profile "${profile}" >"${STATE}/firefox.log" 2>&1 &
}

case "${1:-}" in
  up)
    compose up -d --wait --build
    echo "Up: MLPA http://127.0.0.1:8080 -> Otari http://127.0.0.1:8100 (dashboard, master key ${OTARI_MASTER_KEY}; ${REPLICAS} replica(s)) -> fakes :9100"
    firefox ;;
  check) compose run --rm --no-deps check ;;
  down) compose down -v --remove-orphans ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
