#!/usr/bin/env python3
"""Dictée vocale — interface GTK4 / libadwaita.

Design épuré : un bouton micro central, un sélecteur de mode, la dernière
transcription, et des réglages intégrés (modèle, langue, lancement au démarrage).
Le moteur (enregistrement + transcription + IA) est piloté via dictate.sh.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio  # noqa: E402

DIR = os.path.expanduser("~/.local/share/whisper-dictation")
SCRIPT = os.path.join(DIR, "dictate.sh")
VENV_PY = os.path.join(DIR, "venv", "bin", "python")
DAEMON = os.path.join(DIR, "daemon.py")
SOCK = os.path.join(DIR, "daemon.sock")
CONFIG = os.path.join(DIR, "config.json")
PIDFILE = os.path.join(DIR, "recording.pid")
LASTFILE = os.path.join(DIR, "last.txt")
STATEFILE = os.path.join(DIR, "state")
HISTORY = os.path.join(DIR, "history.jsonl")
APP_ID = "org.stelwey.WhisperDictation"
AUTOSTART = os.path.expanduser("~/.config/autostart/whisper-dictation.desktop")
DESKTOP_SRC = os.path.expanduser(
    "~/.local/share/applications/whisper-dictation.desktop")

CSS = """
.record-btn {
    min-width: 128px;
    min-height: 128px;
    border-radius: 999px;
    background: @accent_bg_color;
    color: @accent_fg_color;
    box-shadow: 0 6px 20px alpha(@accent_bg_color, 0.45);
    transition: box-shadow 250ms ease, background 250ms ease;
}
.record-btn:hover {
    box-shadow: 0 8px 26px alpha(@accent_bg_color, 0.60);
}
.record-btn.recording {
    background: @destructive_bg_color;
    color: @destructive_fg_color;
    box-shadow: 0 6px 20px alpha(@destructive_bg_color, 0.45);
    animation: wd-pulse 1.7s ease-out infinite;
}
@keyframes wd-pulse {
    0%   { box-shadow: 0 0 0 0 alpha(@destructive_bg_color, 0.55); }
    70%  { box-shadow: 0 0 0 22px alpha(@destructive_bg_color, 0); }
    100% { box-shadow: 0 0 0 0 alpha(@destructive_bg_color, 0); }
}
.record-btn.busy {
    background: #e5a50a;
    color: #241c00;
    box-shadow: 0 6px 20px alpha(#e5a50a, 0.45);
    animation: wd-breathe 1.3s ease-in-out infinite;
}
.record-btn.rewrite {
    background: #9141ac;
    color: #ffffff;
    box-shadow: 0 6px 20px alpha(#9141ac, 0.45);
    animation: wd-breathe 1.3s ease-in-out infinite;
}
.record-btn.done {
    background: #2ec27e;
    color: #ffffff;
    box-shadow: 0 6px 20px alpha(#2ec27e, 0.45);
}
.record-btn.errstate {
    background: @destructive_bg_color;
    color: @destructive_fg_color;
}
@keyframes wd-breathe {
    0%   { opacity: 1; }
    50%  { opacity: 0.55; }
    100% { opacity: 1; }
}
.status-title { font-size: 1.25rem; font-weight: 700; }
.mode-seg button { padding: 8px 6px; }
.mode-seg image { margin-bottom: 3px; }
.transcript-card {
    background: @card_bg_color;
    border-radius: 14px;
    padding: 14px 16px;
}
.transcript-text { font-size: 1.02rem; }
.muted { opacity: 0.62; }
"""


# ---------- config ----------
def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {"modes": {}, "current_mode": ""}


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG)


def set_config_key(key, value):
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def _parse_replacements(text):
    """« k8s=Kubernetes, git hub=GitHub » -> dict."""
    reps = {}
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k:
                reps[k] = v.strip()
    return reps


# ---------- moteur ----------
def run_script(*args):
    try:
        subprocess.Popen([SCRIPT, *args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("erreur lancement script:", e)


def is_recording():
    return os.path.exists(PIDFILE)


def read_last():
    try:
        with open(LASTFILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def read_state():
    try:
        with open(STATEFILE) as f:
            return f.read().strip() or "idle"
    except Exception:
        return "idle"


def daemon_alive():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(SOCK)
        s.sendall(json.dumps({"cmd": "ping"}).encode())
        s.recv(1024)
        s.close()
        return True
    except Exception:
        return False


def prewarm_daemon():
    """Démarre le process daemon (sans charger le modèle) pour accélérer la 1re dictée."""
    if daemon_alive():
        return
    try:
        subprocess.Popen([VENV_PY, DAEMON],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        print("prewarm daemon:", e)


def restart_daemon():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(SOCK)
        s.sendall(json.dumps({"cmd": "stop"}).encode())
        s.close()
    except Exception:
        pass
    GLib.timeout_add(500, lambda: (prewarm_daemon(), False)[1])


# ---------- fenêtre principale ----------
class DictateWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="MegaWhisper")
        self.set_default_size(380, 600)

        self._syncing = False
        self._mode_buttons = {}
        self._last_seen = None
        self._last_state = None
        self._state_since = 0.0
        self._shown_visual = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Historique", "win.history")
        menu.append("Réglages", "win.prefs")
        menu.append("À propos", "win.about")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)
        toolbar.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=360, tightening_threshold=320)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(8)
        box.set_margin_bottom(24)
        box.set_margin_start(20)
        box.set_margin_end(20)
        clamp.set_child(box)

        # --- bouton micro ---
        self.rec_btn = Gtk.Button()
        self.rec_btn.set_halign(Gtk.Align.CENTER)
        self.rec_btn.add_css_class("record-btn")
        self.rec_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.rec_icon.set_pixel_size(46)
        self.rec_btn.set_child(self.rec_icon)
        self.rec_btn.connect("clicked", self.on_toggle)
        btn_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        btn_wrap.set_margin_top(24)
        btn_wrap.set_margin_bottom(4)
        btn_wrap.append(self.rec_btn)
        box.append(btn_wrap)

        # --- statut ---
        self.status_lbl = Gtk.Label(label="Prêt à dicter")
        self.status_lbl.add_css_class("status-title")
        box.append(self.status_lbl)

        self.hint_lbl = Gtk.Label(label="Touche le micro ou appuie sur Super + Z")
        self.hint_lbl.add_css_class("muted")
        self.hint_lbl.set_wrap(True)
        self.hint_lbl.set_justify(Gtk.Justification.CENTER)
        box.append(self.hint_lbl)

        # --- sélecteur de mode (segmenté) ---
        seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        seg.add_css_class("linked")
        seg.add_css_class("mode-seg")
        seg.set_halign(Gtk.Align.CENTER)
        seg.set_homogeneous(True)
        seg.set_margin_top(4)
        cfg = load_config()
        group = None
        for key, mode in cfg.get("modes", {}).items():
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            content.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(mode.get("icon", ""))
            img.set_pixel_size(18)
            lbl = Gtk.Label(label=mode.get("label", key))
            lbl.add_css_class("caption")
            content.append(img)
            content.append(lbl)
            btn = Gtk.ToggleButton()
            btn.set_child(content)
            btn.set_size_request(96, -1)
            if group is None:
                group = btn
            else:
                btn.set_group(group)
            btn.connect("toggled", self.on_mode_toggled, key)
            self._mode_buttons[key] = btn
            seg.append(btn)
        box.append(seg)

        self.mode_desc = Gtk.Label(label="")
        self.mode_desc.add_css_class("muted")
        self.mode_desc.add_css_class("caption")
        self.mode_desc.set_wrap(True)
        self.mode_desc.set_justify(Gtk.Justification.CENTER)
        box.append(self.mode_desc)

        # --- carte transcription ---
        head_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head_row.set_margin_top(8)
        th = Gtk.Label(label="Dernière transcription", xalign=0)
        th.add_css_class("heading")
        th.set_hexpand(True)
        th.set_halign(Gtk.Align.START)
        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_btn.add_css_class("flat")
        self.copy_btn.set_tooltip_text("Recopier dans le presse-papier")
        self.copy_btn.connect("clicked", self.on_copy)
        head_row.append(th)
        head_row.append(self.copy_btn)
        box.append(head_row)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("transcript-card")
        card.set_vexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.last_lbl = Gtk.Label(label="—")
        self.last_lbl.add_css_class("transcript-text")
        self.last_lbl.set_wrap(True)
        self.last_lbl.set_xalign(0)
        self.last_lbl.set_yalign(0)
        self.last_lbl.set_selectable(True)
        scroll.set_child(self.last_lbl)
        card.append(scroll)
        box.append(card)

        toolbar.set_content(clamp)
        self.set_content(toolbar)

        # actions
        self._add_action("prefs", self.open_prefs)
        self._add_action("history", self.open_history)
        self._add_action("about", self.open_about)

        self.refresh()
        GLib.timeout_add(500, self._tick)

    def _add_action(self, name, cb):
        act = Gio.SimpleAction.new(name, None)
        act.connect("activate", lambda *_: cb())
        self.add_action(act)

    # ---------- actions principales ----------
    def on_toggle(self, _btn):
        run_script("toggle")
        GLib.timeout_add(200, self.refresh_once)

    def on_mode_toggled(self, btn, key):
        if self._syncing or not btn.get_active():
            return
        set_config_key("current_mode", key)
        self.refresh()

    def on_copy(self, _btn):
        text = read_last()
        if text:
            Gdk.Display.get_default().get_clipboard().set(text)
            self.copy_btn.set_icon_name("emblem-ok-symbolic")
            GLib.timeout_add(
                1200, lambda: (self.copy_btn.set_icon_name("edit-copy-symbolic"), False)[1])

    # ---------- rafraîchissement ----------
    def refresh_once(self):
        self.refresh()
        return GLib.SOURCE_REMOVE

    def _tick(self):
        self.refresh()
        return GLib.SOURCE_CONTINUE

    # état -> (classe CSS, icône, titre, indice)
    STATE_VISUAL = {
        "idle":         ("", "audio-input-microphone-symbolic",
                         "Prêt à dicter", "Touche le micro ou appuie sur Super + Z"),
        "recording":    ("recording", "media-playback-stop-symbolic",
                         "Enregistrement…", "Touche pour arrêter"),
        "transcribing": ("busy", "emblem-synchronizing-symbolic",
                         "Transcription…", "Conversion de la voix en texte"),
        "rewriting":    ("rewrite", "emblem-synchronizing-symbolic",
                         "Amélioration…", "L'IA peaufine le texte"),
        "done":         ("done", "emblem-ok-symbolic",
                         "Copié", "Le texte est dans le presse-papier"),
        "error":        ("errstate", "dialog-error-symbolic",
                         "Erreur", "Une erreur est survenue"),
    }
    _STATE_CLASSES = ["recording", "busy", "rewrite", "done", "errstate"]

    def _apply_visual(self, state):
        if state == self._shown_visual:
            if state == "recording":
                self._update_elapsed()
            return
        self._shown_visual = state
        cls, icon, title, hint = self.STATE_VISUAL.get(
            state, self.STATE_VISUAL["idle"])
        for c in self._STATE_CLASSES:
            self.rec_btn.remove_css_class(c)
        if cls:
            self.rec_btn.add_css_class(cls)
        self.rec_icon.set_from_icon_name(icon)
        self.status_lbl.set_text(title)
        self.hint_lbl.set_text(hint)

    def _update_elapsed(self):
        try:
            elapsed = int(time.time() - os.path.getmtime(PIDFILE))
            self.hint_lbl.set_text(
                f"{elapsed // 60}:{elapsed % 60:02d}  ·  touche pour arrêter")
        except Exception:
            pass

    def refresh(self):
        state = read_state()
        now = time.monotonic()
        if state != self._last_state:
            self._last_state = state
            self._state_since = now
        shown = state
        if state in ("done", "error") and now - self._state_since > 1.6:
            shown = "idle"
        self._apply_visual(shown)

        cfg = load_config()
        current = cfg.get("current_mode", "")
        self._syncing = True
        for key, btn in self._mode_buttons.items():
            btn.set_active(key == current)
        self._syncing = False
        mode = cfg.get("modes", {}).get(current, {})
        self.mode_desc.set_text(mode.get("description", ""))

        text = read_last()
        if text != self._last_seen:
            self._last_seen = text
            self.last_lbl.set_text(text if text else "—")

    # ---------- réglages ----------
    def open_prefs(self):
        cfg = load_config()
        win = Adw.PreferencesWindow(transient_for=self, modal=True)
        win.set_title("Réglages")
        win.set_search_enabled(False)
        self._prefs_win = win

        page = Adw.PreferencesPage()

        # --- Transcription ---
        grp = Adw.PreferencesGroup(title="Transcription")
        engine_row = Adw.ComboRow(title="Moteur",
                                  subtitle="Où et comment la voix est transcrite")
        engine_labels = [
            "Cloud · Groq (large-v3-turbo · rapide)",
            "Local · Précis (medium)",
            "Local · Équilibré (small)",
            "Local · Rapide (base)",
        ]
        engine_specs = [("groq", None), ("local", "medium"),
                        ("local", "small"), ("local", "base")]
        engine_row.set_model(Gtk.StringList.new(engine_labels))
        if cfg.get("transcribe_backend") == "groq":
            cur_idx = 0
        else:
            cur_idx = {"medium": 1, "small": 2, "base": 3}.get(cfg.get("model", "medium"), 1)
        engine_row.set_selected(cur_idx)

        def on_engine(row, _):
            backend, model = engine_specs[row.get_selected()]
            set_config_key("transcribe_backend", backend)
            if backend == "local":
                set_config_key("model", model)
                restart_daemon()
        engine_row.connect("notify::selected", on_engine)
        grp.add(engine_row)

        lang_row = Adw.ComboRow(title="Langue")
        lang_labels = ["Détection auto", "Français", "English"]
        lang_codes = ["", "fr", "en"]
        lang_row.set_model(Gtk.StringList.new(lang_labels))
        cur_lang = cfg.get("language", "")
        lang_row.set_selected(lang_codes.index(cur_lang) if cur_lang in lang_codes else 0)
        lang_row.connect("notify::selected",
                         lambda row, _: set_config_key("language", lang_codes[row.get_selected()]))
        grp.add(lang_row)
        page.add(grp)

        # --- Cloud (Groq) ---
        grp_groq = Adw.PreferencesGroup(
            title="Cloud (Groq)",
            description="Clé gratuite sur console.groq.com/keys — stockée localement, jamais partagée")
        groq_row = Adw.PasswordEntryRow(title="Clé API Groq")
        groq_row.set_text(cfg.get("groq_api_key", ""))
        groq_row.set_show_apply_button(True)
        groq_row.connect("apply",
                         lambda row: set_config_key("groq_api_key", row.get_text().strip()))
        grp_groq.add(groq_row)
        test_row = Adw.ActionRow(title="Tester la clé",
                                 subtitle="Vérifie la connexion à Groq")
        test_btn = Gtk.Button(label="Tester", valign=Gtk.Align.CENTER)
        test_btn.add_css_class("flat")
        test_btn.connect("clicked",
                         lambda _b: self._test_groq_key(groq_row.get_text().strip()))
        test_row.add_suffix(test_btn)
        test_row.set_activatable_widget(test_btn)
        grp_groq.add(test_row)
        page.add(grp_groq)

        # --- IA (amélioration du texte) ---
        grp_ia = Adw.PreferencesGroup(
            title="Amélioration du texte (IA)",
            description="Pour les modes Propre et Prompt")
        ia_row = Adw.ComboRow(
            title="Moteur IA",
            subtitle="Groq = quasi instantané · Local = privé mais lent sur CPU")
        ia_labels = ["Cloud · Groq (llama-3.1-8b · rapide)",
                     "Local · Ollama (qwen3.5:4b)"]
        ia_codes = ["groq", "ollama"]
        ia_row.set_model(Gtk.StringList.new(ia_labels))
        cur_ia = cfg.get("llm_backend", "ollama")
        ia_row.set_selected(ia_codes.index(cur_ia) if cur_ia in ia_codes else 1)
        ia_row.connect("notify::selected",
                       lambda row, _: set_config_key("llm_backend", ia_codes[row.get_selected()]))
        grp_ia.add(ia_row)
        page.add(grp_ia)

        # --- Vocabulaire ---
        grp_vocab = Adw.PreferencesGroup(
            title="Vocabulaire",
            description="Aide la reconnaissance de ton jargon technique")
        vocab = cfg.get("vocabulary", [])
        vocab_row = Adw.EntryRow(title="Mots-clés (séparés par des virgules)")
        vocab_row.set_text(", ".join(vocab) if isinstance(vocab, list) else str(vocab))
        vocab_row.set_show_apply_button(True)
        vocab_row.connect("apply", lambda row: set_config_key(
            "vocabulary", [t.strip() for t in row.get_text().split(",") if t.strip()]))
        grp_vocab.add(vocab_row)
        rep = cfg.get("replacements", {}) or {}
        rep_row = Adw.EntryRow(title="Remplacements (k8s=Kubernetes, …)")
        rep_row.set_text(", ".join(f"{k}={v}" for k, v in rep.items()))
        rep_row.set_show_apply_button(True)
        rep_row.connect("apply",
                        lambda row: set_config_key("replacements", _parse_replacements(row.get_text())))
        grp_vocab.add(rep_row)
        page.add(grp_vocab)

        # --- Pendant la dictée ---
        grp2 = Adw.PreferencesGroup(title="Pendant la dictée")
        sound_row = Adw.SwitchRow(
            title="Sons de début et de fin",
            subtitle="Un son quand ça démarre, un autre quand le texte est prêt")
        sound_row.set_active(cfg.get("sounds", True))
        sound_row.connect("notify::active",
                          lambda row, _: set_config_key("sounds", row.get_active()))
        grp2.add(sound_row)

        vol_adj = Gtk.Adjustment(lower=0, upper=100, step_increment=5,
                                 value=cfg.get("sound_volume", 100))
        vol_row = Adw.SpinRow(title="Volume des sons", adjustment=vol_adj)
        vol_adj.connect("value-changed",
                        lambda a: set_config_key("sound_volume", int(a.get_value())))
        grp2.add(vol_row)

        mute_row = Adw.SwitchRow(
            title="Couper le son pendant que je parle",
            subtitle="Mute les vidéos/sons en cours, puis les rétablit")
        mute_row.set_active(cfg.get("mute_while_speaking", False))
        mute_row.connect("notify::active",
                         lambda row, _: set_config_key("mute_while_speaking", row.get_active()))
        grp2.add(mute_row)
        page.add(grp2)

        # --- Intégration ---
        grp3 = Adw.PreferencesGroup(title="Intégration")
        paste_row = Adw.SwitchRow(
            title="Coller automatiquement au curseur",
            subtitle="Nécessite ydotool + un raccourci global (sinon : copié dans le presse-papier)")
        paste_row.set_active(cfg.get("auto_paste", False))
        paste_row.connect("notify::active",
                          lambda row, _: set_config_key("auto_paste", row.get_active()))
        grp3.add(paste_row)

        sc_row = Adw.ActionRow(title="Raccourci global Super + Z",
                               subtitle="Dicter depuis n'importe quelle application")
        sc_btn = Gtk.Button(label="Configurer", valign=Gtk.Align.CENTER)
        sc_btn.add_css_class("flat")
        sc_btn.connect("clicked", lambda _b: self._setup_shortcut())
        sc_row.add_suffix(sc_btn)
        sc_row.set_activatable_widget(sc_btn)
        grp3.add(sc_row)

        auto_row = Adw.SwitchRow(title="Lancer au démarrage",
                                 subtitle="Ouvre l'app à l'ouverture de session")
        auto_row.set_active(os.path.exists(AUTOSTART))
        auto_row.connect("notify::active",
                         lambda row, _: self.set_autostart(row.get_active()))
        grp3.add(auto_row)
        page.add(grp3)

        win.add(page)
        win.present()

    def _toast_prefs(self, msg):
        win = getattr(self, "_prefs_win", None)
        if win is not None:
            try:
                win.add_toast(Adw.Toast.new(msg))
            except Exception:
                pass

    def _test_groq_key(self, key):
        if not key:
            self._toast_prefs("Saisis d'abord une clé")
            return
        self._toast_prefs("Test en cours…")

        def worker():
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "MegaWhisper/1.0"})
            try:
                urllib.request.urlopen(req, timeout=15).read()
                msg = "Clé valide ✓"
            except urllib.error.HTTPError as e:
                msg = f"Clé refusée (HTTP {e.code})"
            except Exception as e:
                msg = f"Échec de connexion : {e}"
            GLib.idle_add(self._toast_prefs, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _setup_shortcut(self):
        base = "org.gnome.settings-daemon.plugins.media-keys"
        sub_schema = base + ".custom-keybinding"
        path = ("/org/gnome/settings-daemon/plugins/media-keys/"
                "custom-keybindings/megawhisper/")
        try:
            src = Gio.SettingsSchemaSource.get_default()
            if src is None or src.lookup(base, True) is None \
                    or src.lookup(sub_schema, True) is None:
                self._toast_prefs("Raccourcis GNOME indisponibles — à configurer à la main")
                return
            media = Gio.Settings.new(base)
            keys = list(media.get_strv("custom-keybindings"))
            if path not in keys:
                keys.append(path)
                media.set_strv("custom-keybindings", keys)
            sub = Gio.Settings.new_with_path(sub_schema, path)
            sub.set_string("name", "Dictée vocale")
            sub.set_string("command", SCRIPT + " toggle")
            sub.set_string("binding", "<Super>z")
            self._toast_prefs("Raccourci Super + Z configuré ✓")
        except Exception as e:
            self._toast_prefs(f"Échec : {e}")

    def open_history(self):
        win = Adw.Window(transient_for=self, modal=True)
        win.set_title("Historique")
        win.set_default_size(440, 580)
        toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(10)
        box.set_margin_bottom(14)
        box.set_margin_start(12)
        box.set_margin_end(12)
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Rechercher dans l'historique…")
        box.append(search)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(listbox)
        box.append(scroll)
        tv.set_content(box)
        toasts.set_child(tv)
        win.set_content(toasts)

        entries = self._load_history()
        rows = []
        for e in entries:
            txt = e.get("text", "")
            preview = (txt[:90] + "…") if len(txt) > 90 else (txt or "(vide)")
            row = Adw.ActionRow(title=preview)
            row.set_subtitle(f'{e.get("ts", "")} · {e.get("mode", "")} · {e.get("backend", "")}')
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.set_tooltip_text("Copier")
            copy.connect("clicked", lambda _b, t=txt: (
                Gdk.Display.get_default().get_clipboard().set(t),
                toasts.add_toast(Adw.Toast.new("Copié"))))
            row.add_suffix(copy)
            row.set_activatable_widget(copy)
            listbox.append(row)
            rows.append((txt.lower() + " " + e.get("mode", "").lower(), row))

        if not entries:
            listbox.append(Adw.ActionRow(title="Aucune dictée pour l'instant"))

        def on_search(_e):
            q = search.get_text().lower()
            for hay, row in rows:
                row.set_visible(q in hay)
        search.connect("search-changed", on_search)

        win.present()

    def _load_history(self, limit=200):
        out = []
        try:
            with open(HISTORY) as f:
                lines = f.readlines()
            for line in reversed(lines[-limit:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
        return out

    def set_autostart(self, enabled):
        try:
            if enabled:
                os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
                content = (
                    "[Desktop Entry]\nType=Application\nName=MegaWhisper\n"
                    f"Exec=/usr/bin/python3 {os.path.join(DIR, 'app.py')}\n"
                    "Icon=org.stelwey.WhisperDictation\nTerminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n")
                with open(AUTOSTART, "w") as f:
                    f.write(content)
            else:
                if os.path.exists(AUTOSTART):
                    os.remove(AUTOSTART)
        except Exception as e:
            print("autostart:", e)

    def open_about(self):
        about = Gtk.AboutDialog(transient_for=self, modal=True)
        about.set_program_name("MegaWhisper")
        about.set_version("3.0")
        about.set_comments("Dictée vocale rapide et privée\n(Whisper + Ollama, ou Groq en option)")
        about.set_logo_icon_name("org.stelwey.WhisperDictation")
        about.present()


class DictateApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        prewarm_daemon()

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = DictateWindow(self)
        win.present()


if __name__ == "__main__":
    DictateApp().run(None)
