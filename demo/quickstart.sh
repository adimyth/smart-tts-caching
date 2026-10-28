#!/usr/bin/env bash
# Try the cache on a live call, from a fresh clone, in one command:
#
#     ./demo/quickstart.sh                      # talk to the demo agent yourself (Hinglish)
#     ./demo/quickstart.sh --lang en            # the same agent in English
#     ./demo/quickstart.sh --prompt my.txt      # your own system prompt
#     ./demo/quickstart.sh --mode scripted      # the launch video's calls; needs only a Sarvam key
#     ./demo/quickstart.sh --mode pure          # the LLM words every line itself, so fewer repeat
#     ./demo/quickstart.sh --no-level-c         # Levels A and B only; skips the 1.6 GB of Level C models
#
# It installs uv if it is missing, clones the pinned Pipecat beside this repository (where the
# workspace and CI expect it), runs `uv sync`, starts Redis on port 6380 (redis-server, else
# Docker), asks for the API keys it needs and saves them to .env, then runs demo/try.py with the
# arguments given here: the agent starts on http://127.0.0.1:7860 and the call page opens.
# macOS and Linux.

set -euo pipefail

PIPECAT_REPO=https://github.com/pipecat-ai/pipecat.git
PIPECAT_REF=1d1385b6d5e3375199f98b2d456e56472bee16d2  # the commit pyproject.toml and CI pin
REDIS_PORT=6380

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPECAT_DIR="$(cd "$ROOT/.." && pwd)/pipecat"
cd "$ROOT"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Tools.
command -v git >/dev/null || die "git is needed"
if ! command -v uv >/dev/null; then
  [ -x "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null; then
  [ -t 0 ] || die "uv is needed: curl -LsSf https://astral.sh/uv/install.sh | sh"
  read -rp "uv is not installed. Install it now from astral.sh? [Y/n] " ok
  case "${ok:-y}" in [Yy]*) ;; *) die "uv is needed: https://docs.astral.sh/uv/" ;; esac
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
# hnswlib (Level B's index) ships as source only, so uv sync compiles it.
command -v c++ >/dev/null || command -v g++ >/dev/null || command -v clang++ >/dev/null \
  || die "a C++ compiler is needed to build hnswlib: xcode-select --install (macOS) or apt install build-essential"

# 2. The pinned Pipecat, beside this repository.
if [ ! -e "$PIPECAT_DIR" ]; then
  say "Cloning Pipecat ${PIPECAT_REF:0:9} into $PIPECAT_DIR"
  git clone --quiet --filter=blob:none "$PIPECAT_REPO" "$PIPECAT_DIR"
  git -C "$PIPECAT_DIR" -c advice.detachedHead=false checkout --quiet "$PIPECAT_REF"
elif [ "$(git -C "$PIPECAT_DIR" rev-parse HEAD 2>/dev/null)" != "$PIPECAT_REF" ]; then
  die "$PIPECAT_DIR exists but is not Pipecat at ${PIPECAT_REF:0:9}; check that commit out there (git -C $PIPECAT_DIR checkout $PIPECAT_REF) or move it aside"
fi

# 3. Python 3.12 and the dependencies.
say "Installing the dependencies (uv sync)"
uv sync

# 4. Redis on $REDIS_PORT.
redis_ok() { .venv/bin/python -c "import redis; redis.Redis(port=$REDIS_PORT, socket_connect_timeout=1).ping()" 2>/dev/null; }
if ! redis_ok; then
  if command -v redis-server >/dev/null; then
    say "Starting Redis on port $REDIS_PORT"
    redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --maxmemory 256mb --maxmemory-policy allkeys-lfu \
      --save "" --daemonize yes >/dev/null
  elif command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    say "Starting Redis in Docker on port $REDIS_PORT (container tts-cache-redis)"
    docker run -d --rm --name tts-cache-redis -p "127.0.0.1:$REDIS_PORT:6379" redis:7-alpine \
      redis-server --maxmemory 256mb --maxmemory-policy allkeys-lfu --save "" >/dev/null
  else
    die "Redis is needed: brew install redis (macOS), apt install redis-server (Linux), or start Docker; then run this again"
  fi
  for _ in $(seq 20); do redis_ok && break; sleep 0.5; done
  redis_ok || die "Redis did not come up on port $REDIS_PORT"
fi

# 5. API keys: Sarvam for speech-to-text and the voice, OpenAI for the LLM (not in scripted mode).
mode=hybrid
prev=
for arg in "$@"; do
  [ "$prev" = --mode ] && mode=$arg
  case "$arg" in --mode=*) mode=${arg#--mode=} ;; esac
  prev=$arg
done
need=(SARVAM_API_KEY)
[ "$mode" = scripted ] || need+=(OPENAI_API_KEY)
has_key() { [ -n "${!1:-}" ] || grep -qE "^(export )?$1=.+" .env 2>/dev/null; }
for key in "${need[@]}"; do
  has_key "$key" && continue
  case "$key" in
    SARVAM_API_KEY) where="https://dashboard.sarvam.ai" ;;
    OPENAI_API_KEY) where="https://platform.openai.com/api-keys" ;;
  esac
  [ -t 0 ] || die "$key is needed: add it to .env or export it (get one at $where)"
  read -rsp "$key (from $where): " value
  echo
  [ -n "$value" ] || die "$key is needed"
  [ -e .env ] || { touch .env; chmod 600 .env; }
  [ -s .env ] && [ -n "$(tail -c1 .env)" ] && echo >> .env
  printf '%s=%s\n' "$key" "$value" >> .env
  echo "saved $key to .env"
done

# 6. The agent and the call page.
say "Starting the agent"
exec .venv/bin/python -u demo/try.py "$@"
