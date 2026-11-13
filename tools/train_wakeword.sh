#!/bin/bash
# Train the Atticus "ei siri" / "siri" wake-word models with livekit-wakeword.
# RUN THIS ON A MACHINE WITH AN NVIDIA GPU; training needs CUDA. Detection runs
# on CPU on the Omarchy machine afterwards.
#
#   bash train_wakeword.sh
#
# It installs the trainer, trains both models, and leaves them in ./models/.
# You then copy ei_siri.onnx (and siri.onnx) to the Omarchy PC's
# ~/atticus-linux/models/ .  See tools/train_wakeword.md for the full guide.
set -e
cd "$(dirname "$0")"
mkdir -p models

echo "== 1/4  uv (package runner) =="
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "== 2/4  livekit-wakeword trainer (pulls torch+CUDA) =="
uv tool install --python 3.13 "livekit-wakeword[train]" || uv tool upgrade livekit-wakeword

# --- LiveKit Cloud credentials (free plan; used only to generate training speech) ---
# Easiest: create a free project at https://cloud.livekit.io , open Settings -> Keys,
# and export the three values below (or drop them in a .env.local file next to this script).
if [ -f .env.local ]; then set -a; . ./.env.local; set +a; fi
if [ -z "$LIVEKIT_API_KEY" ] || [ -z "$LIVEKIT_API_SECRET" ] || [ -z "$LIVEKIT_URL" ]; then
  if command -v lk >/dev/null; then
    echo "== LiveKit Cloud login (browser) =="
    lk cloud auth && lk app env -w && { set -a; . ./.env.local; set +a; }
  fi
fi
if [ -z "$LIVEKIT_API_KEY" ]; then
  cat <<'MSG'

!! Missing LiveKit Cloud credentials. Do this once:
   1) Sign up free at https://cloud.livekit.io  (no card needed)
   2) Project Settings -> Keys -> create a key
   3) export them and re-run this script:
        export LIVEKIT_URL="wss://YOUR-project.livekit.cloud"
        export LIVEKIT_API_KEY="API..."
        export LIVEKIT_API_SECRET="secret..."
      (or put those 3 lines in a file named .env.local next to this script)
MSG
  exit 1
fi

# LiveKit Cloud free plan limits concurrent TTS; 5 (the default) hits HTTP 429.
# Keep it low (2) so synthesis doesn't rate-limit. Override: WAKE_CONCURRENCY=3 bash ...
CONC="${WAKE_CONCURRENCY:-2}"

train() {   # $1 = phrase, $2 = expected onnx basename (phrase with spaces->_), rest = extra setup flags
  local phrase="$1" name="$2"; shift 2
  echo "== training '$phrase' (TTS concurrency=$CONC) =="
  livekit-wakeword setup --wakeword "$phrase" --language pt --concurrency "$CONC" "$@" --eval
  livekit-wakeword train --wakeword "$phrase" --out ./models
  # the output is named after the phrase (spaces -> underscores)
  livekit-wakeword eval  --wakeword "$phrase" --model "./models/${name}.onnx" 2>/dev/null \
    || livekit-wakeword eval --wakeword "$phrase" 2>/dev/null || true
}

echo "== 3/4  train 'ei siri' (recommended - longer = fewer false triggers) =="
train "ei siri" "ei_siri" \
  --hard-negative "ei" --hard-negative "aí" --hard-negative "ei você" --hard-negative "série"

echo "== 4/4  train 'siri' (shorter alternative) =="
train "siri" "siri" \
  --hard-negative "série" --hard-negative "ciranda" --hard-negative "siriema" --hard-negative "cedo"

echo
echo "Done. Models:"
ls -la models/*.onnx 2>/dev/null || echo "  (no .onnx produced - check the eval output above)"
cat <<'MSG'

Next: copy a model to the Omarchy PC, e.g.:
    scp models/ei_siri.onnx  user@YOUR-OMARCHY-PC:~/atticus-linux/models/
Then on Omarchy: set "wakeWord":"ei siri" in ~/.config/atticus/settings.json and
restart Atticus. It will print 'wake word on (livekit, trained model)'.
MSG
