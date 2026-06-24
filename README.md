<h1 align="center">Dictée vocale</h1>

<p align="center">
  Dictée vocale <strong>locale et privée</strong> pour Linux / GNOME — vous parlez, le texte arrive dans le presse-papier.
  <br>
  <em>Local, private voice dictation for Linux / GNOME (Whisper + Ollama).</em>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white">
  <img alt="GTK4" src="https://img.shields.io/badge/GTK4-libadwaita-4A86CF?logo=gnome&logoColor=white">
  <img alt="Plateforme Linux" src="https://img.shields.io/badge/Plateforme-Linux-FCC624?logo=linux&logoColor=black">
  <img alt="Licence MIT" src="https://img.shields.io/badge/Licence-MIT-green">
</p>

Vous parlez, [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcrit en local,
et — au choix — un modèle local servi par [Ollama](https://ollama.com) corrige ou reformule
le texte. Le résultat atterrit directement dans le presse-papier. Interface GTK4 / libadwaita,
soignée et minimale. Tout fonctionne **hors-ligne** ; un moteur cloud (Groq) reste disponible
en option, avec **repli automatique** vers le moteur local en cas d'échec.

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Utilisation](#utilisation)
- [Les modes](#les-modes)
- [Configuration](#configuration)
- [Développement](#développement)
- [Licence](#licence)

## Fonctionnalités

- **Local et privé** — l'audio et la transcription restent sur votre machine ; rien n'est envoyé sur le réseau (sauf si vous activez explicitement le moteur cloud Groq).
- **Trois modes** — *Brut* (texte tel quel), *Propre* (correction des fautes et des mots mal transcrits d'après le contexte) et *Prompt* (reformulation en prompt clair pour une IA).
- **Whisper + Ollama** — transcription par faster-whisper, post-traitement optionnel par un modèle local via Ollama (`qwen3.5:4b` par défaut).
- **Option cloud Groq** — transcription distante rapide et précise (`whisper-large-v3-turbo`), avec repli automatique vers le local si la clé manque ou que l'appel échoue.
- **Sons de début et de fin** — un bip au démarrage de l'enregistrement, un autre quand le texte est prêt.
- **Mute automatique** — coupe (en option) les sorties audio en cours pendant que vous parlez, puis les rétablit.
- **Lancement au démarrage** — activable depuis les réglages (entrée d'autostart GNOME).
- **Daemon mémoire** — le modèle Whisper est gardé en RAM entre deux dictées puis déchargé après inactivité, pour éviter de recharger plusieurs centaines de Mo à chaque fois.

## Architecture

```
app.py        Interface GTK4 : bouton micro, sélecteur de mode, réglages.
  │           Lit l'état (fichier `state`) et `last.txt` ; écrit `config.json`.
  ▼
dictate.sh    Lanceur : active le venv et appelle dictate.py.
  ▼
dictate.py    Cœur : enregistre (arecord) → transcrit → post-traite (Ollama)
  │           → copie (wl-copy). Pilote l'état via des fichiers.
  ▼
daemon.py     Garde le modèle Whisper en RAM (socket Unix) et le décharge
              après inactivité, pour ne pas le recharger à chaque dictée.
```

`transcribe.py` est une version autonome et historique (transcription d'un fichier
WAV en ligne de commande) ; elle n'est pas utilisée par le flux principal, qui
passe par le daemon.

### Communication par fichiers

Tout transite par le dossier d'installation `~/.local/share/whisper-dictation/` :

| Fichier         | Rôle                                                          |
|-----------------|--------------------------------------------------------------|
| `config.json`   | Réglages + définition des modes (voir `config.example.json`) |
| `state`         | État courant (idle / recording / transcribing / rewriting / done / error) |
| `last.txt`      | Dernière transcription (affichée dans l'app)                 |
| `recording.pid` | Présent = enregistrement en cours                            |
| `daemon.sock`   | Socket Unix pour parler au daemon                            |

## Prérequis

Outils système (à installer via votre gestionnaire de paquets) :

- **`arecord`** — capture micro (paquet `alsa-utils`)
- **`paplay`** et **`pactl`** — sons et mute des sorties audio (paquet `pulseaudio-utils`)
- **`wl-copy`** — copie vers le presse-papier Wayland (paquet `wl-clipboard`)
- **Python 3** avec les bindings **GTK4 / libadwaita** (`python3-gi`, `gtk4`, `libadwaita`)

Python : **`faster-whisper`** (installé dans le `venv`, voir `requirements.txt`).

Optionnel : [**Ollama**](https://ollama.com) avec le modèle `qwen3.5:4b` pour les
modes *Propre* et *Prompt* (`ollama pull qwen3.5:4b`).

> L'app utilise `wl-copy`, donc elle vise une session **Wayland** (cas par défaut de GNOME récent).

## Installation

```bash
git clone https://github.com/cmrabdu/MegaWhisper.git
cd MegaWhisper
./install.sh
```

Le script `install.sh` crée l'environnement virtuel et installe `faster-whisper`,
relie le code à l'installation (`~/.local/share/whisper-dictation/`) via des liens
symboliques, copie `config.example.json` vers `config.json` (sans écraser une config
existante) et installe le lanceur `.desktop`. Lancez ensuite **Dictée vocale** depuis
le menu des applications.

## Utilisation

1. Ouvrez **Dictée vocale**.
2. Touchez le bouton micro pour démarrer l'enregistrement, retouchez-le pour l'arrêter.
3. Le texte est transcrit (puis amélioré selon le mode) et **copié dans le presse-papier** — il ne reste plus qu'à le coller (`Ctrl + V`).

La dernière transcription est affichée dans l'app et recopiable d'un clic.

Le cœur est aussi pilotable en ligne de commande via `dictate.sh` :

```bash
./dictate.sh            # toggle : démarre puis arrête + transcrit
./dictate.sh toggle     # idem
./dictate.sh cycle      # passe au mode suivant
./dictate.sh mode propre
./dictate.sh get-mode   # affiche le mode courant
./dictate.sh status     # "recording" ou "idle"
```

**Raccourci clavier global** — l'app n'enregistre pas de raccourci système elle-même.
Pour déclencher la dictée par une combinaison (par ex. `Super + Z`), créez un raccourci
personnalisé dans **Paramètres GNOME → Clavier → Raccourcis personnalisés** pointant vers
`~/.local/share/whisper-dictation/dictate.sh toggle`.

## Les modes

| Mode       | IA  | Effet                                                       |
|------------|-----|-------------------------------------------------------------|
| **Brut**   | non | Texte tel quel, sans retouche.                              |
| **Propre** | oui | Corrige fautes + mots manifestement mal transcrits d'après le contexte. |
| **Prompt** | oui | Reformate la dictée en prompt clair et structuré pour une IA. |

Les modes *Propre* et *Prompt* nécessitent Ollama. Les prompts système de chaque
mode sont définis dans `config.json` (`modes.*.system`) et entièrement modifiables.

## Configuration

La configuration est copiée depuis le template au premier lancement de `install.sh` ;
pour la (re)créer manuellement :

```bash
cp config.example.json ~/.local/share/whisper-dictation/config.json
```

Tout se règle ensuite depuis l'app (menu → **Réglages**) : moteur de transcription
(local *small / medium / base*, ou cloud Groq), langue, modèle IA, sons, mute pendant
la dictée, lancement au démarrage. La **clé API Groq** (optionnelle) se saisit dans les
réglages et reste stockée localement dans `config.json` — elle n'est **pas** versionnée.

## Développement

Le code vit dans ce dépôt ; l'app installée pointe dessus via des liens symboliques.
Une modification ici est donc active au prochain lancement de l'app.

```bash
# (Re)lier l'app installée à ce dépôt — idempotent.
./link-dev.sh
```

`link-dev.sh` remplace les fichiers de code de `~/.local/share/whisper-dictation/`
par des liens symboliques vers ce dépôt (en sauvegardant l'original en `*.orig` la
première fois). Les données lourdes ou privées ne sont **pas versionnées** (voir
`.gitignore`) car régénérables : `venv/`, `models/`, `config.json`, `*.wav`, `state`,
logs…

## Licence

Distribué sous licence **MIT**. Voir le fichier [`LICENSE`](LICENSE).
