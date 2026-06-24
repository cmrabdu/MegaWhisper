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
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
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
TIMINGS = os.path.join(DIR, "timings.log")
# Certaines API derrière Cloudflare (Groq) rejettent le User-Agent par défaut de
# Python (« Python-urllib ») avec une erreur 403. On en envoie un explicite.
USER_AGENT = "MegaWhisper/1.0"
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


GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_llm(text, mode, cfg):
    """Correction/reformulation via Groq (cloud, rapide) — même rôle qu'ollama_process."""
    key = cfg.get("groq_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Groq manquante")
    model = cfg.get("groq_llm_model", "llama-3.1-8b-instant")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": mode.get("system", "")},
            {"role": "user", "content": f"<texte>\n{text}\n</texte>"},
        ],
        "temperature": 0.1,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(GROQ_CHAT_URL, data=data, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return _clean_llm_output(out["choices"][0]["message"]["content"])


def llm_improve(text, mode, cfg):
    """Améliore le texte via le backend choisi : 'groq' (cloud, rapide) ou 'ollama'
    (local). Repli automatique sur Ollama si le cloud échoue."""
    if cfg.get("llm_backend", "ollama") == "groq" and cfg.get("groq_api_key", "").strip():
        try:
            return groq_llm(text, mode, cfg)
        except Exception as e:
            notify(f"IA cloud indisponible — bascule en local. ({e})", "normal")
    return ollama_process(text, mode, cfg)


# ---------- transcription cloud (Groq, optionnelle) ----------
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _opus_path(wav):
    """Transcode le WAV en Opus (petit) pour un upload cloud léger et fiable.
    Retourne le chemin .ogg, ou None si ffmpeg est absent ou échoue."""
    if not shutil.which("ffmpeg"):
        return None
    out = wav + ".ogg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "20k", out],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30)
        return out if os.path.exists(out) and os.path.getsize(out) > 0 else None
    except Exception:
        return None


def groq_transcribe(wav, cfg, prompt=""):
    key = cfg.get("groq_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Groq manquante")
    model = cfg.get("groq_model", "whisper-large-v3-turbo")
    lang = cfg.get("language", "") or "fr"
    # Upload compressé en Opus (~15x plus léger) : plus rapide, et évite les
    # coupures de connexion sur les longues dictées. Repli sur le WAV brut.
    opus = _opus_path(wav)
    path, fname, ctype = (opus, "rec.ogg", "audio/ogg") if opus \
        else (wav, "rec.wav", "audio/wav")
    boundary = "----wddictate" + str(os.getpid())

    def field(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    with open(path, "rb") as f:
        audio = f.read()
    body = field("model", model)
    if lang:
        body += field("language", lang)
    if prompt:
        body += field("prompt", prompt)
    body += field("response_format", "json")
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
    body += audio + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": USER_AGENT,
    }
    try:
        last_err = None
        for attempt in range(3):  # réessais sur erreurs transitoires (reset, 5xx)
            try:
                req = urllib.request.Request(GROQ_URL, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                return out.get("text", "").strip()
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                last_err = e
            except urllib.error.URLError as e:
                last_err = e
            time.sleep(0.4 * (attempt + 1))
        raise last_err
    finally:
        if opus:
            try:
                os.remove(opus)
            except Exception:
                pass


# ---------- vocabulaire, contexte, remplacements ----------
def build_hotwords(cfg):
    """Jargon (config 'vocabulary') joint pour biaiser la transcription."""
    vocab = cfg.get("vocabulary", [])
    terms = vocab.split(",") if isinstance(vocab, str) else [str(t) for t in vocab]
    return ", ".join(t.strip() for t in terms if t.strip())


def _clipboard_snippet():
    """Court extrait du presse-papier (une ligne, tronquée), comme indice de contexte."""
    try:
        out = subprocess.check_output(
            ["wl-paste", "-n"], text=True, timeout=1, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    return " ".join(out.split())[:120]


def _active_window_title():
    """Titre de la fenêtre active si le compositeur l'expose en CLI.
    GNOME (Wayland) ne le permet pas ; Sway et Hyprland oui."""
    try:
        if shutil.which("swaymsg"):
            tree = json.loads(subprocess.check_output(
                ["swaymsg", "-t", "get_tree"], text=True, timeout=1))
            stack = [tree]
            while stack:
                node = stack.pop()
                if node.get("focused") and node.get("name"):
                    return node["name"]
                stack.extend(node.get("nodes", []) + node.get("floating_nodes", []))
        elif shutil.which("hyprctl"):
            return json.loads(subprocess.check_output(
                ["hyprctl", "activewindow", "-j"], text=True, timeout=1)).get("title", "")
    except Exception:
        pass
    return ""


def build_context(cfg):
    """Indice de contexte court (fenêtre active + presse-papier) pour guider la
    transcription. Volontairement bref et tronqué : jamais le texte brut entier."""
    if not cfg.get("context_injection", True):
        return ""
    clip = _clipboard_snippet()
    # éviter de réinjecter notre propre sortie : on vient souvent de copier la
    # dictée précédente dans le presse-papier.
    try:
        with open(os.path.join(DIR, "last.txt")) as f:
            last = " ".join(f.read().split())
        if clip and last and clip in last:
            clip = ""
    except Exception:
        pass
    bits = [b for b in (_active_window_title(), clip) if b]
    return " · ".join(bits)[:200]


def apply_replacements(text, cfg):
    """Remplacements littéraux (config 'replacements'), insensibles à la casse,
    sur mots entiers — ex. k8s -> Kubernetes."""
    reps = cfg.get("replacements", {})
    if not reps or not text:
        return text
    import re
    for pat, rep in reps.items():
        if pat:
            text = re.sub(r"\b" + re.escape(pat) + r"\b", rep, text, flags=re.IGNORECASE)
    return text


def do_transcribe(cfg):
    """Transcrit selon le moteur choisi ; repli local si le cloud échoue.
    Le vocabulaire (jargon) guide les deux moteurs ; l'indice de contexte
    (presse-papier / fenêtre) n'est utilisé qu'en local, pour rester privé.
    Retourne (texte, moteur_réellement_utilisé)."""
    backend = cfg.get("transcribe_backend", "local")
    hotwords = build_hotwords(cfg)
    if backend == "groq" and cfg.get("groq_api_key", "").strip():
        try:
            return groq_transcribe(WAV, cfg, prompt=hotwords), "groq"
        except Exception as e:
            notify(f"Groq indisponible — bascule en local. ({e})", "normal")
            # repli local
    ensure_daemon()
    resp = daemon_request({
        "cmd": "transcribe", "wav": WAV, "lang": cfg.get("language", ""),
        "hotwords": hotwords or None,
        "initial_prompt": build_context(cfg) or None,
        "beam_size": int(cfg.get("beam_size", 5)),
    })
    return resp.get("text", "").strip(), "local"


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


def _audio_duration(wav):
    """Durée (s) du WAV — pour comparer le temps de traitement au temps de parole."""
    try:
        import wave
        with wave.open(wav) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def log_timing(cfg, used_backend, audio_s, text, t_transcribe, t_llm, t_total):
    """Journalise le temps de chaque étape dans timings.log (pour profiler la lenteur).
    used_backend = le moteur RÉELLEMENT utilisé (groq ou local, après repli éventuel)."""
    model = cfg.get("groq_model", "groq") if used_backend == "groq" else cfg.get("model", "?")
    rt = (t_total / audio_s) if audio_s else 0.0
    line = (f'{time.strftime("%Y-%m-%d %H:%M:%S")} | parle={audio_s:.0f}s '
            f'texte={len(text)}c | transcription={t_transcribe:.1f}s '
            f'ia={t_llm:.1f}s total={t_total:.1f}s | '
            f'moteur={used_backend}/{model} beam={cfg.get("beam_size", 1)} '
            f'ia={cfg.get("llm_backend", "ollama")} | '
            f'vitesse={rt:.2f}x_du_temps_de_parole\n')
    try:
        with open(TIMINGS, "a") as f:
            f.write(line)
    except Exception:
        pass


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
    audio_s = _audio_duration(WAV)
    set_state("transcribing")
    notify("Transcription en cours…", icon=mode_icon)
    t_start = time.monotonic()
    try:
        text, used_backend = do_transcribe(cfg)
    except Exception as e:
        set_state("error")
        notify(f"Erreur transcription: {e}", "critical")
        return
    t_transcribe = time.monotonic() - t_start
    if not text:
        set_state("idle")
        notify("Rien entendu", icon=mode_icon)
        return

    # remplacements de vocabulaire (k8s -> Kubernetes, etc.) avant l'IA
    text = apply_replacements(text, cfg)

    # post-traitement IA selon le mode (groq = rapide, ollama = local)
    t_llm = 0.0
    if mode.get("llm"):
        set_state("rewriting")
        notify(f"Amélioration ({mode.get('label', '')})…", icon=mode_icon)
        t_llm0 = time.monotonic()
        try:
            cleaned = llm_improve(text, mode, cfg)
            if cleaned:
                text = cleaned
        except Exception as e:
            notify(f"IA indisponible — texte brut conservé. ({e})", "normal")
        t_llm = time.monotonic() - t_llm0

    to_clipboard(text)
    try:
        with open(os.path.join(DIR, "last.txt"), "w") as f:
            f.write(text)
    except Exception:
        pass
    log_timing(cfg, used_backend, audio_s, text, t_transcribe, t_llm,
               time.monotonic() - t_start)
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
