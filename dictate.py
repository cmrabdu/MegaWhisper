#!/usr/bin/env python3
"""Cœur de la dictée vocale.

Usage :
  dictate.py            -> toggle (1er appel = enregistre, 2e = transcrit+colle)
  dictate.py mode NAME  -> change le mode courant
  dictate.py cycle      -> passe au mode suivant
  dictate.py get-mode   -> affiche le mode courant (label)
  dictate.py list-modes -> liste les modes (json)
  dictate.py status     -> "recording" ou "idle"
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

DIR = os.path.expanduser("~/.local/share/whisper-dictation")
VENV_PY = os.path.join(DIR, "venv", "bin", "python")
DAEMON = os.path.join(DIR, "daemon.py")
SOCK = os.path.join(DIR, "daemon.sock")
WAV = os.path.join(DIR, "recording.wav")
PIDFILE = os.path.join(DIR, "recording.pid")
CONFIG = os.path.join(DIR, "config.json")
STATEFILE = os.path.join(DIR, "state")
MUTEFILE = os.path.join(DIR, "muted_sinks.json")
SOUNDS = os.path.join(DIR, "sounds")
OLLAMA_URL = "http://localhost:11434/api/generate"


# ---------- sons (début / fin de dictée) ----------
def play_sound(name, block=False):
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if not cfg.get("sounds", True):
        return
    path = os.path.join(SOUNDS, name)
    if not os.path.exists(path):
        return
    try:
        if block:
            subprocess.run(["paplay", path], check=False)
        else:
            subprocess.Popen(["paplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


# ---------- config ----------
def load_config():
    with open(CONFIG) as f:
        return json.load(f)


# ---------- état (remplace les pop-ups de notification) ----------
def set_state(state):
    """Écrit l'état courant : idle / recording / transcribing / rewriting / done / error.
    Les interfaces (app, extension) lisent ce fichier pour animer leur icône."""
    try:
        tmp = STATEFILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(state)
        os.replace(tmp, STATEFILE)
    except Exception:
        pass


# ---------- mute audio pendant la dictée ----------
def _sink_names():
    out = subprocess.check_output(["pactl", "list", "short", "sinks"], text=True)
    names = []
    for line in out.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    return names


def mute_outputs():
    """Mute toutes les sorties non déjà mutées ; mémorise lesquelles pour les restaurer."""
    muted = []
    try:
        for name in _sink_names():
            try:
                st = subprocess.check_output(
                    ["pactl", "get-sink-mute", name], text=True).strip().lower()
            except Exception:
                continue
            if "no" in st:  # n'était pas mutée → on la mute et on la note
                subprocess.run(["pactl", "set-sink-mute", name, "1"], check=False)
                muted.append(name)
    except Exception:
        pass
    try:
        with open(MUTEFILE, "w") as f:
            json.dump(muted, f)
    except Exception:
        pass


def restore_outputs():
    """Restaure (démute) uniquement les sorties qu'on avait coupées."""
    try:
        with open(MUTEFILE) as f:
            muted = json.load(f)
    except Exception:
        muted = []
    for name in muted:
        subprocess.run(["pactl", "set-sink-mute", name, "0"], check=False)
    try:
        os.remove(MUTEFILE)
    except Exception:
        pass


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG)


# ---------- notifications (désactivables ; l'état passe surtout par l'icône) ----------
def notify(msg, urgency="normal", icon="audio-input-microphone-symbolic"):
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if not cfg.get("notifications", False):
        return
    try:
        subprocess.run(
            ["notify-send", "-t", "2500", "-u", urgency,
             "-i", icon, "-a", "Dictée vocale", "Dictée vocale", msg],
            check=False,
        )
    except FileNotFoundError:
        pass


# ---------- daemon ----------
def daemon_request(req, timeout=120):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall(json.dumps(req).encode("utf-8"))
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode("utf-8"))


def ensure_daemon():
    # déjà vivant ?
    try:
        daemon_request({"cmd": "ping"}, timeout=2)
        return
    except Exception:
        pass
    # le démarrer, détaché
    subprocess.Popen(
        [VENV_PY, DAEMON],
        stdout=open(os.path.join(DIR, "daemon.log"), "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # attendre qu'il réponde
    for _ in range(50):
        time.sleep(0.2)
        try:
            daemon_request({"cmd": "ping"}, timeout=2)
            return
        except Exception:
            continue


# ---------- post-traitement IA ----------
def _clean_llm_output(text):
    """Retire les enrobages fréquents que le modèle ajoute malgré la consigne."""
    # retire un éventuel bloc de réflexion <think>…</think> (modèles type qwen3)
    if "<think>" in text:
        import re
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text.replace("<think>", "").replace("</think>", "")
    text = text.strip()
    # guillemets entourant toute la réponse
    if len(text) >= 2 and text[0] in "\"«“'" and text[-1] in "\"»”'":
        text = text[1:-1].strip()
    # préfixes parasites en début de réponse
    low = text.lower()
    for pref in ("corrigé :", "corrigé:", "texte corrigé :", "texte corrigé:",
                 "voici le prompt :", "voici le prompt:", "prompt :", "prompt:",
                 "voici le texte corrigé :", "réponse :", "réponse:"):
        if low.startswith(pref):
            text = text[len(pref):].strip()
            break
    return text


OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"


def ollama_process(text, mode, cfg):
    model = cfg.get("ollama_model", "qwen3.5:4b")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": mode.get("system", "")},
            {"role": "user", "content": f"<texte>\n{text}\n</texte>"},
        ],
        "stream": False,
        "keep_alive": cfg.get("ollama_keep_alive", "5m"),
        "options": {"temperature": 0.1},
    }
    # modèles à "réflexion" (qwen3/3.5…) : mode direct via le paramètre think
    if "qwen3" in model:
        payload["think"] = False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_CHAT_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return _clean_llm_output(out.get("message", {}).get("content", ""))


# ---------- transcription cloud (Groq, optionnelle) ----------
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def groq_transcribe(wav, cfg):
    key = cfg.get("groq_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Groq manquante")
    model = cfg.get("groq_model", "whisper-large-v3-turbo")
    lang = cfg.get("language", "") or "fr"
    boundary = "----wddictate" + str(os.getpid())

    def field(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    with open(wav, "rb") as f:
        audio = f.read()
    body = field("model", model)
    if lang:
        body += field("language", lang)
    body += field("response_format", "json")
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="rec.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
    body += audio + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out.get("text", "").strip()


def do_transcribe(cfg):
    """Transcrit selon le moteur choisi ; repli local si le cloud échoue."""
    backend = cfg.get("transcribe_backend", "local")
    if backend == "groq" and cfg.get("groq_api_key", "").strip():
        try:
            return groq_transcribe(WAV, cfg)
        except Exception as e:
            notify(f"Groq indisponible — bascule en local. ({e})", "normal")
            # repli local
    ensure_daemon()
    resp = daemon_request(
        {"cmd": "transcribe", "wav": WAV, "lang": cfg.get("language", "")})
    return resp.get("text", "").strip()


# ---------- presse-papier ----------
def to_clipboard(text):
    p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


# ---------- actions ----------
def is_recording():
    return os.path.exists(PIDFILE)


def start_recording():
    cfg = load_config()
    play_sound("start.wav", block=True)  # avant le mute, pour rester audible
    if cfg.get("mute_while_speaking", False):
        mute_outputs()
    set_state("recording")
    notify("Enregistrement en cours — réappuie pour arrêter",
           icon="media-record-symbolic")
    proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", WAV]
    )
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))


def stop_and_transcribe():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except Exception:
        pid = None
    os.remove(PIDFILE)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.3)

    cfg = load_config()
    # le micro s'arrête → on a fini de parler → on remet le son
    if cfg.get("mute_while_speaking", False):
        restore_outputs()

    mode_name = cfg.get("current_mode", "brut")
    mode = cfg["modes"].get(mode_name, {})

    mode_icon = mode.get("icon", "audio-input-microphone-symbolic")
    set_state("transcribing")
    notify("Transcription en cours…", icon=mode_icon)
    try:
        text = do_transcribe(cfg)
    except Exception as e:
        set_state("error")
        notify(f"Erreur transcription: {e}", "critical")
        return
    if not text:
        set_state("idle")
        notify("Rien entendu", icon=mode_icon)
        return

    # post-traitement IA selon le mode
    if mode.get("llm"):
        set_state("rewriting")
        notify(f"Amélioration ({mode.get('label', '')})…", icon=mode_icon)
        try:
            cleaned = ollama_process(text, mode, cfg)
            if cleaned:
                text = cleaned
        except Exception as e:
            notify(f"IA indisponible — texte brut conservé. ({e})", "normal")

    to_clipboard(text)
    try:
        with open(os.path.join(DIR, "last.txt"), "w") as f:
            f.write(text)
    except Exception:
        pass
    set_state("done")
    play_sound("done.wav")  # le texte est prêt à coller
    preview = text[:80] + ("…" if len(text) > 80 else "")
    notify(f"Copié — {mode.get('label', '')}\n{preview}",
           icon="edit-copy-symbolic")


def cmd_mode(name):
    cfg = load_config()
    if name in cfg["modes"]:
        cfg["current_mode"] = name
        save_config(cfg)
        m = cfg["modes"][name]
        notify(f"Mode : {m.get('label', name)}",
               icon=m.get("icon", "audio-input-microphone-symbolic"))
    else:
        notify(f"Mode inconnu : {name}", "critical")


def cmd_cycle():
    cfg = load_config()
    names = list(cfg["modes"].keys())
    cur = cfg.get("current_mode", names[0])
    nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
    cfg["current_mode"] = nxt
    save_config(cfg)
    m = cfg["modes"][nxt]
    notify(f"Mode : {m.get('label', nxt)}",
           icon=m.get("icon", "audio-input-microphone-symbolic"))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "toggle"

    if arg == "toggle":
        if is_recording():
            stop_and_transcribe()
        else:
            start_recording()
    elif arg == "mode" and len(sys.argv) > 2:
        cmd_mode(sys.argv[2])
    elif arg == "cycle":
        cmd_cycle()
    elif arg == "get-mode":
        cfg = load_config()
        m = cfg["modes"].get(cfg.get("current_mode", ""), {})
        print(m.get("label", "?"))
    elif arg == "list-modes":
        cfg = load_config()
        print(json.dumps({
            "current": cfg.get("current_mode"),
            "modes": {k: v.get("label", k) for k, v in cfg["modes"].items()},
        }, ensure_ascii=False))
    elif arg == "status":
        print("recording" if is_recording() else "idle")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
