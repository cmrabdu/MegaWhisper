#!/usr/bin/env python3
"""Daemon de transcription : garde le modèle faster-whisper en RAM et le
décharge après une période d'inactivité pour libérer la mémoire.

Protocole (socket Unix, une requête JSON par ligne) :
  -> {"cmd": "transcribe", "wav": "/chemin/vers.wav", "lang": "fr"}
  <- {"text": "..."}
  -> {"cmd": "ping"}                 <- {"ok": true, "loaded": false}
  -> {"cmd": "stop"}                 (arrête le daemon)
"""
import json
import os
import socket
import sys
import threading
import time

DIR = os.path.expanduser("~/.local/share/whisper-dictation")
SOCK = os.path.join(DIR, "daemon.sock")
CACHE_DIR = os.path.join(DIR, "models")
CONFIG = os.path.join(DIR, "config.json")


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


class ModelHolder:
    """Charge le modèle à la demande, le décharge après inactivité."""

    def __init__(self, size, idle_seconds, cpu_threads=8):
        self.size = size
        self.idle_seconds = idle_seconds
        self.cpu_threads = cpu_threads
        self._model = None
        self._last_used = 0.0
        self._lock = threading.Lock()
        t = threading.Thread(target=self._reaper, daemon=True)
        t.start()

    def get(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                sys.stderr.write(
                    f"[daemon] chargement du modèle {self.size} "
                    f"({self.cpu_threads} threads)…\n")
                self._model = WhisperModel(
                    self.size, device="cpu", compute_type="int8",
                    download_root=CACHE_DIR, cpu_threads=self.cpu_threads,
                    num_workers=1,
                )
            self._last_used = time.monotonic()
            return self._model

    def loaded(self):
        return self._model is not None

    def _reaper(self):
        while True:
            time.sleep(15)
            with self._lock:
                if self._model is not None and self.idle_seconds > 0:
                    if time.monotonic() - self._last_used > self.idle_seconds:
                        sys.stderr.write("[daemon] inactif → déchargement du modèle (RAM libérée)\n")
                        self._model = None


def transcribe(holder, wav, lang):
    model = holder.get()
    segments, _ = model.transcribe(
        wav, language=(lang or None), vad_filter=True, beam_size=1,
        condition_on_previous_text=False, without_timestamps=True,
    )
    return "".join(seg.text for seg in segments).strip()


def handle(conn, holder):
    try:
        data = conn.recv(65536).decode("utf-8").strip()
        if not data:
            return
        req = json.loads(data)
        cmd = req.get("cmd")
        if cmd == "ping":
            resp = {"ok": True, "loaded": holder.loaded()}
        elif cmd == "stop":
            conn.sendall(json.dumps({"ok": True}).encode())
            os._exit(0)
        elif cmd == "transcribe":
            text = transcribe(holder, req["wav"], req.get("lang", ""))
            resp = {"text": text}
        else:
            resp = {"error": f"commande inconnue: {cmd}"}
    except Exception as e:
        resp = {"error": str(e)}
    try:
        conn.sendall(json.dumps(resp).encode("utf-8"))
    except Exception:
        pass
    finally:
        conn.close()


def main():
    cfg = load_config()
    size = cfg.get("model", "small")
    idle = int(cfg.get("idle_unload_seconds", 300))
    threads = int(cfg.get("cpu_threads", 8))

    if os.path.exists(SOCK):
        os.remove(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(4)
    holder = ModelHolder(size, idle, threads)
    sys.stderr.write(
        f"[daemon] prêt sur {SOCK} (modèle {size}, {threads} threads, "
        f"déchargement après {idle}s)\n")

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, holder), daemon=True).start()


if __name__ == "__main__":
    main()
