#!/usr/bin/env sh
# Lance Nexbot et le relance automatiquement après *restart (code de sortie 3).
cd "$(dirname "$0")" || exit 1
while true; do
  python3 -m nexbot.bot
  code=$?
  if [ "$code" -ne 3 ]; then
    echo "Nexbot est arrêté (code $code)."
    exit "$code"
  fi
  echo "Redémarrage de Nexbot…"
  sleep 2
done
