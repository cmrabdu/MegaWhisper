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
HISTORY = os.path.join(DIR, "history.jsonl")
# Certaines API derrière Cloudflare (Groq) rejettent le User-Agent par défaut de
# Python (« Python-urllib ») avec une erreur 403. On en envoie un explicite.
USER_AGENT = "MegaWhisper/1.0"
OLLAMA_URL = "http://localhost:11434/api/generate"
# Modèles Groq par défaut (vérifiés le 22/09/2026 sur de vraies dictées) :
# large-v3 est plus fidèle que turbo en français (turbo saute parfois une phrase) ;
# qwen3.8-27b corrige sans reformuler, là où gpt-oss réécrit trop.
DEFAULT_GROQ_STT = "whisper-large-v3"
DEFAULT_GROQ_LLM = "qwen/qwen3.8-27b"
# Replis tentés dans l'ordre si le modèle configuré échoue ou a été retiré
# (llama-3.1-8b-instant a disparu de Groq sans prévenir : le mode Propre
# renvoyait le brut en silence pendant des semaines).
GROQ_STT_FALLBACKS = ["whisper-large-v3", "whisper-large-v3-turbo"]
GROQ_LLM_FALLBACKS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
# Hallucinations classiques de Whisper sur un silence ou un clip très court.
HALLUCINATIONS = {
    "merci", "merci.", "merci !", "merci beaucoup.", "thank you", "thank you.",
    "thanks for watching!", "merci d'avoir regardé.", "sous-titrage st' 501",
    "sous-titres réalisés para la communauté d'amara.org",
    "sous-titres réalisés par la communauté d'amara.org", "you", "bye.", "au revoir.",
}


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
        vol = max(0, min(100, int(cfg.get("sound_volume", 100))))
    except Exception:
        vol = 100
    args = ["paplay", f"--volume={int(vol / 100 * 65536)}", path]
    try:
        if block:
            subprocess.run(args, check=False)
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
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
def notify(msg, urgency="normal", icon="audio-input-microphone-symbolic", force=False):
    """force=True : affiché même si les notifications sont coupées — réservé aux
    problèmes que l'utilisateur doit savoir (IA en panne, micro inaccessible…)."""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if not force and not cfg.get("notifications", False):
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
            {"role": "system", "content": _system_prompt(mode, cfg)},
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


def _system_prompt(mode, cfg):
    """Prompt système du mode + le vocabulaire de l'utilisateur, pour que l'IA
    sache écrire « Groq » là où Whisper a entendu « groc »."""
    system = mode.get("system", "")
    vocab = build_hotwords(cfg)
    if vocab:
        system += ("\n\nVocabulaire de l'utilisateur (à écrire exactement ainsi quand "
                   f"un mot mal transcrit y ressemble) : {vocab}.")
    return system


def _model_chain(configured, fallbacks):
    chain = [configured] if configured else []
    return chain + [m for m in fallbacks if m not in chain]


def groq_llm(text, mode, cfg, model=None):
    """Correction/reformulation via Groq (cloud, rapide) — même rôle qu'ollama_process."""
    key = cfg.get("groq_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Groq manquante")
    model = model or cfg.get("groq_llm_model", DEFAULT_GROQ_LLM)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(mode, cfg)},
            {"role": "user", "content": f"<texte>\n{text}\n</texte>"},
        ],
        "temperature": 0.1,
        "stream": False,
        # une dictée de 10 min ≈ 2-3k tokens : ne jamais couper la réponse
        "max_completion_tokens": max(2048, len(text)),
    }
    # modèles à raisonnement : réponse directe, sans réflexion (latence, troncature)
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"
        payload["include_reasoning"] = False
    elif "qwen3" in model:
        payload["reasoning_effort"] = "none"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(GROQ_CHAT_URL, data=data, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    choice = out["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("réponse tronquée")
    return _clean_llm_output(choice["message"]["content"])


def _http_err(e):
    """Message lisible d'une erreur HTTP d'API (corps JSON « error.message »)."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = json.loads(e.read().decode())
            msg = (body.get("error") or {}).get("message") if isinstance(
                body.get("error"), dict) else None
            return f"HTTP {e.code} : " + str(msg or body.get("message") or body.get("detail"))
        except Exception:
            return f"HTTP {e.code}"
    return str(e)


def llm_improve(text, mode, cfg):
    """Améliore le texte via le backend choisi : 'groq' (cloud, rapide) ou 'ollama'
    (local). Chaque modèle Groq de la chaîne est essayé, puis Ollama.
    Retourne (texte, moteur_utilisé) ; lève une exception si tout a échoué."""
    errors = []
    if cfg.get("llm_backend", "ollama") == "groq" and cfg.get("groq_api_key", "").strip():
        for model in _model_chain(cfg.get("groq_llm_model"), GROQ_LLM_FALLBACKS):
            try:
                return groq_llm(text, mode, cfg, model), f"groq/{model}"
            except Exception as e:
                errors.append(f"{model} : {_http_err(e)}")
    try:
        return ollama_process(text, mode, cfg), f"ollama/{cfg.get('ollama_model', '?')}"
    except Exception as e:
        errors.append(f"ollama : {e}")
    raise RuntimeError(" | ".join(errors))


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


def _cloud_transcribe(url, key, fields, wav):
    """POST multipart OpenAI-compatible (/v1/audio/transcriptions), commun à Groq
    et Mistral. fields = liste de (nom, valeur) ; un nom peut se répéter."""
    # Upload compressé en Opus (~15x plus léger) : plus rapide, et évite les
    # coupures de connexion sur les longues dictées. Repli sur le WAV brut.
    opus = _opus_path(wav)
    path, fname, ctype = (opus, "rec.ogg", "audio/ogg") if opus \
        else (wav, "rec.wav", "audio/wav")
    boundary = "----wddictate" + str(os.getpid())
    with open(path, "rb") as f:
        audio = f.read()
    body = b"".join(
        (f'--{boundary}\r\nContent-Disposition: form-data; '
         f'name="{name}"\r\n\r\n{value}\r\n').encode()
        for name, value in fields if value)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
    body += audio + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": USER_AGENT,
    }
    # ~1 s de calcul par minute d'audio côté serveur : large marge pour l'upload
    timeout = 60 + len(audio) / 20000
    try:
        last_err = None
        for attempt in range(3):  # réessais sur erreurs transitoires (reset, 429, 5xx)
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                return out.get("text", "").strip()
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
            time.sleep(0.6 * (attempt + 1))
        raise last_err
    finally:
        if opus:
            try:
                os.remove(opus)
            except Exception:
                pass


def groq_transcribe(wav, cfg, prompt="", model=None):
    key = cfg.get("groq_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Groq manquante")
    model = model or cfg.get("groq_model", DEFAULT_GROQ_STT)
    return _cloud_transcribe(GROQ_URL, key, [
        ("model", model), ("language", cfg.get("language", "") or "fr"),
        ("prompt", prompt), ("response_format", "json")], wav)


MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"


def mistral_transcribe(wav, cfg):
    """Mistral Voxtral : meilleurs scores publiés en français (sept. 2026).
    Pas de « prompt » : le vocabulaire passe par context_bias (≤ 100 termes,
    un champ par terme)."""
    key = cfg.get("mistral_api_key", "").strip()
    if not key:
        raise RuntimeError("clé API Mistral manquante")
    vocab = [t.strip() for t in build_hotwords(cfg).split(",") if t.strip()][:100]
    fields = [("model", cfg.get("mistral_model", "voxtral-mini-latest"))]
    if cfg.get("language"):
        fields.append(("language", cfg["language"]))
    fields += [("context_bias", t) for t in vocab]
    return _cloud_transcribe(MISTRAL_URL, key, fields, wav)


# ---------- vocabulaire, contexte, remplacements ----------
def build_hotwords(cfg):
    """Jargon (config 'vocabulary') joint pour biaiser la transcription."""
    vocab = cfg.get("vocabulary", [])
    terms = vocab.split(",") if isinstance(vocab, str) else [str(t) for t in vocab]
    return ", ".join(t.strip() for t in terms if t.strip())


def build_whisper_prompt(cfg):
    """Prompt pour Whisper. Whisper imite le STYLE du prompt : une vraie phrase
    ponctuée dans la langue de la dictée donne une sortie ponctuée et bien
    orthographiée (mesuré : « groc » → « Groq », ponctuation correcte)."""
    vocab = build_hotwords(cfg)
    if cfg.get("language", "fr") == "en":
        head = "Dictation in English, with technical terms"
    else:
        head = "Dictée en français, avec des termes techniques en anglais"
    return (f"{head} : {vocab}." if vocab else head + ".")[:800]


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


def do_transcribe(cfg, wav=WAV):
    """Transcrit selon le moteur choisi ; repli local si le cloud échoue.
    Le vocabulaire (jargon) guide les deux moteurs ; l'indice de contexte
    (presse-papier / fenêtre) n'est utilisé qu'en local, pour rester privé.
    Retourne (texte, moteur_réellement_utilisé)."""
    backend = cfg.get("transcribe_backend", "local")
    hotwords = build_hotwords(cfg)
    if backend == "mistral" and cfg.get("mistral_api_key", "").strip():
        try:
            return (mistral_transcribe(wav, cfg),
                    f"mistral/{cfg.get('mistral_model', 'voxtral-mini-latest')}")
        except Exception as e:
            notify(f"Mistral indisponible — repli sur Groq/local.\n{_http_err(e)}",
                   "normal", force=True)
            backend = "groq"  # repli : Groq s'il y a une clé, sinon local
    if backend == "groq" and cfg.get("groq_api_key", "").strip():
        errors = []
        for model in _model_chain(cfg.get("groq_model"), GROQ_STT_FALLBACKS):
            try:
                # Pas de prompt par défaut : mesuré le 22/09/2026, un prompt de
                # vocabulaire fait SAUTER des phrases entières à Whisper (jusqu'à
                # la moitié d'un clip) voire inventer « merci d'avoir regardé ».
                # Le jargon est corrigé ensuite par l'IA, qui reçoit le vocabulaire.
                prompt = build_whisper_prompt(cfg) if cfg.get("whisper_prompt") else ""
                return (groq_transcribe(wav, cfg, prompt=prompt, model=model),
                        f"groq/{model}")
            except urllib.error.HTTPError as e:
                errors.append(f"{model} : {_http_err(e)}")
                if e.code in (401, 403):
                    break  # clé refusée : inutile d'essayer un autre modèle
            except Exception as e:
                errors.append(f"{model} : {e}")
                break  # réseau : les autres modèles échoueraient pareil
        notify("Groq indisponible — transcription locale (plus lente).\n"
               + " | ".join(errors), "normal", force=True)
    ensure_daemon()
    resp = daemon_request({
        "cmd": "transcribe", "wav": wav, "lang": cfg.get("language", ""),
        "hotwords": hotwords or None,
        "initial_prompt": build_context(cfg) or None,
        "beam_size": int(cfg.get("beam_size", 5)),
    })
    if "error" in resp and not resp.get("text"):
        raise RuntimeError(resp["error"])
    return resp.get("text", "").strip(), f"local/{cfg.get('model', '?')}"


# ---------- presse-papier ----------
def to_clipboard(text):
    p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def auto_paste(cfg):
    """Colle le texte au curseur (Ctrl+V) si activé et si ydotool est disponible.
    Surtout utile déclenché par un raccourci global (le focus reste sur ton appli)."""
    if not cfg.get("auto_paste", False) or not shutil.which("ydotool"):
        return
    try:
        time.sleep(0.12)  # laisser le presse-papier se mettre en place
        # Ctrl+V via codes d'évènements Linux : LEFTCTRL=29, V=47
        subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                       check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------- actions ----------
def is_recording():
    return os.path.exists(PIDFILE)


def _read_pid():
    try:
        with open(PIDFILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _is_arecord(pid):
    """Vrai si pid est bien NOTRE arecord (et pas un pid recyclé par un autre process)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"arecord" in f.read()
    except Exception:
        return False


def start_recording():
    cfg = load_config()
    play_sound("start.wav", block=True)  # avant le mute, pour rester audible
    if cfg.get("mute_while_speaking", False):
        mute_outputs()
    try:
        os.remove(WAV)  # jamais de reste d'une dictée précédente
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", WAV],
        stderr=subprocess.PIPE, start_new_session=True,
    )
    time.sleep(0.15)
    if proc.poll() is not None:  # micro occupé / absent : le dire tout de suite
        err = proc.stderr.read().decode(errors="replace").strip()
        if cfg.get("mute_while_speaking", False):
            restore_outputs()
        set_state("error")
        notify(f"Micro inaccessible : {err or 'arecord a échoué'}", "critical",
               icon="dialog-error-symbolic", force=True)
        return
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))
    set_state("recording")
    notify("Enregistrement en cours — réappuie pour arrêter",
           icon="media-record-symbolic")


def _audio_duration(wav):
    """Durée (s) du WAV — pour comparer le temps de traitement au temps de parole."""
    try:
        import wave
        with wave.open(wav) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def log_timing(cfg, used_backend, audio_s, text, t_transcribe, t_llm, t_total,
               used_llm="-"):
    """Journalise le temps de chaque étape dans timings.log (pour profiler la lenteur).
    used_backend / used_llm = les moteurs RÉELLEMENT utilisés (après repli éventuel)."""
    rt = (t_total / audio_s) if audio_s else 0.0
    line = (f'{time.strftime("%Y-%m-%d %H:%M:%S")} | parle={audio_s:.0f}s '
            f'texte={len(text)}c | transcription={t_transcribe:.1f}s '
            f'ia={t_llm:.1f}s total={t_total:.1f}s | '
            f'moteur={used_backend} ia={used_llm} | '
            f'vitesse={rt:.2f}x_du_temps_de_parole\n')
    try:
        with open(TIMINGS, "a") as f:
            f.write(line)
    except Exception:
        pass


ARCHIVE_DIR = os.path.join(DIR, "archive")


def archive_recording(cfg):
    """Déplace recording.wav sous un nom horodaté dans archive/ avant tout traitement,
    puis purge les archives plus vieilles que `archive_days` (défaut 14 jours).
    Une dictée ne peut ainsi plus être perdue par l'écrasement de recording.wav
    (y compris si on relance une dictée pendant que la précédente se transcrit)
    ni par une réécriture IA tronquée (vécu le 15/08/2026 : 17 min perdues).
    Retourne le chemin de l'archive, ou None."""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        dest = os.path.join(ARCHIVE_DIR,
                            time.strftime("recording-%Y%m%d-%H%M%S") + ".wav")
        os.replace(WAV, dest)
        keep_s = float(cfg.get("archive_days", 14)) * 86400
        now = time.time()
        for name in os.listdir(ARCHIVE_DIR):
            path = os.path.join(ARCHIVE_DIR, name)
            try:
                if now - os.path.getmtime(path) > keep_s:
                    os.remove(path)
            except Exception:
                pass
        return dest
    except Exception:
        return None


def append_history(text, mode_label, used_backend, raw=None):
    """Ajoute la dictée à l'historique (history.jsonl) : horodatage, mode, moteur, texte,
    et le brut avant réécriture IA s'il diffère (récupérable si l'IA a tronqué)."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode_label,
               "backend": used_backend, "text": text}
        if raw and raw != text:
            rec["raw"] = raw
        with open(HISTORY, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _stop_arecord(pid):
    """Arrête arecord et attend qu'il ait fini d'écrire l'en-tête WAV
    (au lieu d'un sleep fixe qui coupait parfois la fin du fichier)."""
    if not pid or not _is_arecord(pid):
        return
    try:
        os.kill(pid, signal.SIGINT)  # SIGINT : arecord finalise proprement le fichier
    except ProcessLookupError:
        return
    for _ in range(40):  # jusqu'à 2 s
        time.sleep(0.05)
        if not _is_arecord(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _is_hallucination(text, audio_s):
    """Whisper invente « Merci. » / « Thank you » sur un silence ou un clip minuscule."""
    t = text.strip().lower()
    if not any(c.isalnum() for c in t):  # « ... » sur un silence
        return True
    return audio_s < 4 and (t in HALLUCINATIONS or len(t) <= 2)


def stop_and_transcribe():
    pid = _read_pid()
    try:
        os.remove(PIDFILE)
    except FileNotFoundError:
        return  # un autre appel (double appui) s'en occupe déjà
    _stop_arecord(pid)

    cfg = load_config()
    # le micro s'arrête → on a fini de parler → on remet le son
    if cfg.get("mute_while_speaking", False):
        restore_outputs()
    play_sound("stop.wav")  # après le démute, sinon inaudible
    try:
        _process(cfg)
    except Exception as e:  # jamais bloqué en « transcription… » après un plantage
        set_state("error")
        notify(f"Erreur inattendue : {e}", "critical",
               icon="dialog-error-symbolic", force=True)


def _process(cfg):
    mode_name = cfg.get("current_mode", "brut")
    mode = cfg["modes"].get(mode_name, {})
    mode_icon = mode.get("icon", "audio-input-microphone-symbolic")

    if not os.path.exists(WAV):
        set_state("error")
        notify("Aucun audio enregistré (micro coupé ?)", "critical", force=True)
        return
    audio_s = _audio_duration(WAV)
    if audio_s < 0.4:  # appui accidentel : rien à transcrire
        set_state("idle")
        return
    # déplacé dans archive/ AVANT tout traitement ; on transcrit depuis l'archive
    archive_path = archive_recording(cfg)
    wav = archive_path or WAV
    set_state("transcribing")
    notify("Transcription en cours…", icon=mode_icon)
    t_start = time.monotonic()
    try:
        text, used_backend = do_transcribe(cfg, wav)
    except Exception as e:
        set_state("error")
        notify(f"Erreur transcription : {e}\nAudio gardé : {wav}", "critical",
               force=True)
        return
    t_transcribe = time.monotonic() - t_start
    if not text or _is_hallucination(text, audio_s):
        set_state("idle")
        notify("Rien entendu", icon=mode_icon)
        return

    # remplacements de vocabulaire (k8s -> Kubernetes, etc.) avant l'IA
    text = apply_replacements(text, cfg)
    raw_text = text  # brut conservé dans l'historique, quoi qu'il arrive ensuite
    if archive_path:
        try:
            with open(archive_path[:-4] + ".txt", "w") as f:
                f.write(text)
        except Exception:
            pass

    # post-traitement IA selon le mode (groq = rapide, ollama = local)
    t_llm = 0.0
    used_llm = "-"
    if mode.get("llm"):
        set_state("rewriting")
        notify(f"Amélioration ({mode.get('label', '')})…", icon=mode_icon)
        t_llm0 = time.monotonic()
        try:
            cleaned, used_llm = llm_improve(text, mode, cfg)
            # garde-fou : une réécriture nettement plus courte que le brut est une
            # troncature du LLM (vu le 15/08/2026 : 17 min dictées, 45 % rendus).
            # Le mode Prompt condense volontairement : seuil plus bas.
            ratio = 0.35 if mode_name == "prompt" else 0.7
            if cleaned and len(cleaned) < ratio * len(text):
                used_llm += "(rejeté:tronqué)"
                notify("Réécriture tronquée — texte brut conservé.", "normal", force=True)
            elif cleaned:
                text = cleaned
        except Exception as e:
            used_llm = "échec"
            notify(f"IA indisponible — texte brut copié.\n{e}", "normal",
                   icon="dialog-warning-symbolic", force=True)
        t_llm = time.monotonic() - t_llm0

    to_clipboard(text)
    auto_paste(cfg)
    try:
        with open(os.path.join(DIR, "last.txt"), "w") as f:
            f.write(text)
    except Exception:
        pass
    append_history(text, mode.get("label", ""), used_backend, raw=raw_text)
    log_timing(cfg, used_backend, audio_s, text, t_transcribe, t_llm,
               time.monotonic() - t_start, used_llm)
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
            # arecord mort en route (micro débranché…) : on transcrit ce qu'on a
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
