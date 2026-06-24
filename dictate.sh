#!/usr/bin/env bash
# Lanceur de la dictée vocale. Transmet TOUS les arguments à dictate.py
# (toggle par défaut si aucun argument n'est fourni).
DIR="$HOME/.local/share/whisper-dictation"
if [ $# -eq 0 ]; then
    exec "$DIR/venv/bin/python" "$DIR/dictate.py" toggle
else
    exec "$DIR/venv/bin/python" "$DIR/dictate.py" "$@"
fi
