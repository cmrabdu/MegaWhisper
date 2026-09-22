#!/usr/bin/env bash
# Relie l'app installée à ce dépôt (mode développement).
#
# Remplace les fichiers de CODE dans ~/.local/share/whisper-dictation/ par des
# liens symboliques vers ce dépôt. Du coup, toute modification faite ici dans
# MegaWhisper est active dans l'app dès le prochain lancement — pas de copie à
# faire. Les données lourdes (venv, models) et le runtime (config.json, etc.)
# restent dans le dossier de l'app et ne sont PAS touchés.
#
# Idempotent : on peut le relancer sans risque.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/.local/share/whisper-dictation"

CODE_FILES=(app.py daemon.py dictate.py transcribe.py dictate.sh sounds)

echo "Dépôt : $REPO"
echo "App   : $APP"
echo

for f in "${CODE_FILES[@]}"; do
  target="$APP/$f"
  src="$REPO/$f"
  # déjà le bon lien ?
  if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$src" ]; then
    echo "✓ $f (déjà lié)"
    continue
  fi
  # sauvegarde de l'ancien fichier réel, une seule fois
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    if [ -d "$target" ]; then
      # dossier réel (ex. sounds/) : mis de côté, sinon ln le remplirait au lieu de le remplacer
      rm -rf "$target.orig"; mv "$target" "$target.orig"
      echo "  ($f/ mis de côté en $f.orig/)"
    elif [ ! -e "$target.orig" ]; then
      cp -p "$target" "$target.orig"
      echo "  ($f sauvegardé en $f.orig)"
    fi
  fi
  ln -sfn "$src" "$target"
  echo "→ $f lié vers le dépôt"
done

echo
echo "Terminé. Relance l'app (Dictée vocale) pour utiliser le code du dépôt."
