#!/usr/bin/env python3
"""Transcrit un fichier audio WAV en texte avec faster-whisper (local)."""
import sys
import os
from faster_whisper import WhisperModel

# Taille du modèle : tiny / base / small / medium / large-v3
# Surchargeable via la variable d'env WHISPER_MODEL
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
# Langue : "fr", "en"... ou vide pour détection automatique
LANGUAGE = os.environ.get("WHISPER_LANG", "") or None

# Cache du modèle dans le dossier de l'app
CACHE_DIR = os.path.expanduser("~/.local/share/whisper-dictation/models")

def main():
    if len(sys.argv) < 2:
        sys.exit("usage: transcribe.py <fichier.wav>")
    audio = sys.argv[1]

    # int8 sur CPU = rapide et léger
    model = WhisperModel(
        MODEL_SIZE, device="cpu", compute_type="int8", download_root=CACHE_DIR
    )
    segments, _ = model.transcribe(
        audio, language=LANGUAGE, vad_filter=True, beam_size=1
    )
    text = "".join(seg.text for seg in segments).strip()
    sys.stdout.write(text)

if __name__ == "__main__":
    main()
