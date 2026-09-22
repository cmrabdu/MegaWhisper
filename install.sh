#!/usr/bin/env bash
#
# Installation de « MegaWhisper » (Whisper + Ollama).
#
# Met en place le dossier d'installation ~/.local/share/whisper-dictation/ :
#   - crée un environnement virtuel Python et y installe faster-whisper ;
#   - relie le code de ce dépôt à l'installation (liens symboliques) ;
#   - copie le template de configuration s'il n'existe pas encore ;
#   - installe le lanceur .desktop pour qu'il apparaisse dans GNOME.
#
# Idempotent : on peut le relancer sans risque. Les données lourdes
# (venv, models) et le runtime (config.json) ne sont jamais écrasés.
#
# Prérequis système (à installer via le gestionnaire de paquets) :
#   alsa-utils (arecord), pulseaudio-utils (paplay, pactl),
#   wl-clipboard (wl-copy), python3 + python3-venv,
#   et les bindings GTK4/libadwaita (python3-gi, gtk4, libadwaita).
#   Ollama est optionnel, pour les modes « Propre » et « Prompt ».
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/.local/share/whisper-dictation"
APPS_DIR="$HOME/.local/share/applications"

echo "Dépôt : $REPO"
echo "App   : $APP"
echo

# 1) Dossier d'installation
mkdir -p "$APP" "$APPS_DIR"

# 2) Environnement virtuel + dépendances Python
if [ ! -x "$APP/venv/bin/python" ]; then
  echo "→ Création de l'environnement virtuel…"
  python3 -m venv "$APP/venv"
fi
echo "→ Installation des dépendances Python (faster-whisper)…"
"$APP/venv/bin/python" -m pip install --upgrade pip >/dev/null
"$APP/venv/bin/python" -m pip install -r "$REPO/requirements.txt"

# 3) Liens symboliques du code vers l'installation
echo "→ Liaison du code (link-dev.sh)…"
"$REPO/link-dev.sh"

# 4) Configuration (sans écraser une config existante)
if [ ! -f "$APP/config.json" ]; then
  cp "$REPO/config.example.json" "$APP/config.json"
  echo "→ config.json créé depuis config.example.json"
else
  echo "✓ config.json déjà présent (laissé tel quel)"
fi

# 5) Lanceur .desktop (Exec pointe vers l'app installée)
DESKTOP_SRC="$REPO/packaging/whisper-dictation.desktop"
DESKTOP_DST="$APPS_DIR/whisper-dictation.desktop"
sed "s|Exec=.*|Exec=/usr/bin/python3 $APP/app.py|" "$DESKTOP_SRC" > "$DESKTOP_DST"
echo "→ Lanceur installé : $DESKTOP_DST"

echo
echo "Terminé. Lance « MegaWhisper » depuis le menu des applications."
echo "Modes IA (Propre / Prompt) : installe Ollama puis « ollama pull qwen3.5:4b »."
