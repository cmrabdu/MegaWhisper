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
- [Cloud (Groq) — optionnel mais rapide](#cloud-groq--optionnel-mais-rapide)
- [Configuration](#configuration)
- [Journal de performance](#journal-de-performance)
- [Développement](#développement)
- [Licence](#licence)

## Fonctionnalités

- **Local et privé** — l'audio et la transcription restent sur votre machine ; rien n'est envoyé sur le réseau (sauf si vous activez explicitement le moteur cloud Groq).
- **Trois modes** — *Brut* (texte tel quel), *Propre* (correction des fautes et des mots mal transcrits d'après le contexte) et *Prompt* (reformulation en prompt clair pour une IA).
- **Whisper + Ollama** — transcription par faster-whisper, post-traitement optionnel par un modèle local via Ollama (`qwen3.5:4b` par défaut).
- **Moteurs cloud Groq (optionnels, rapides)** — la transcription peut passer par Groq `whisper-large-v3` (ou Mistral Voxtral) et la correction IA par Groq `qwen/qwen3.8-27b` : la dictée est prête en ~1-2 s au lieu de dizaines de secondes en local. **Repli automatique** vers le local (faster-whisper + Ollama) si le cloud échoue ou que la clé manque. Réglé par `transcribe_backend` (`local` / `groq` / `mistral`) et `llm_backend` (`ollama` / `groq`).
- **Compression Opus avant l'envoi cloud** — l'audio est transcodé en Opus via `ffmpeg` avant l'upload (~15× plus léger), ce qui rend la transcription des longues dictées rapide et fiable. Repli sur le WAV brut si `ffmpeg` est absent ; réessais automatiques sur erreurs réseau transitoires.
- **Vocabulaire & remplacements** — une liste de jargon (`vocabulary`) est imposée au modèle de transcription via *hotwords*, et une table de remplacements littéraux (`replacements`, ex. `k8s` → `Kubernetes`) est appliquée après transcription.
- **Injection de contexte** — un court extrait du presse-papier (et le titre de la fenêtre active sous Sway / Hyprland) guide la transcription **en local uniquement** ; cet indice n'est **jamais** envoyé au cloud, par souci de confidentialité.
- **Journal de performance** — chaque dictée écrit une ligne dans `timings.log` (durée de parole, temps de transcription, temps d'IA, total, moteur réellement utilisé, ratio de vitesse).
- **Sons de début, de fin et de prêt** — trois tons doux de la même famille (générés par `sounds/generate.py`).
- **Mute automatique** — coupe (en option) les sorties audio en cours pendant que vous parlez, puis les rétablit.
- **Lancement au démarrage** — activable depuis les réglages ; l'app démarre alors **en arrière-plan, sans fenêtre** (`app.py --hidden`). La fenêtre ne s'ouvre que si on lance MegaWhisper depuis le menu.
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
| `timings.log`   | Journal de performance (une ligne par dictée, voir plus bas) |

## Prérequis

Outils système (à installer via votre gestionnaire de paquets) :

- **`arecord`** — capture micro (paquet `alsa-utils`)
- **`paplay`** et **`pactl`** — sons et mute des sorties audio (paquet `pulseaudio-utils`)
- **`wl-copy`** — copie vers le presse-papier Wayland (paquet `wl-clipboard`)
- **Python 3** avec les bindings **GTK4 / libadwaita** (`python3-gi`, `gtk4`, `libadwaita`)

Python : **`faster-whisper`** (installé dans le `venv`, voir `requirements.txt`).

Recommandé : **`ffmpeg`** (paquet `ffmpeg`) — sert à compresser l'audio en Opus
avant l'envoi au cloud Groq (uploads ~15× plus légers, transcription des longues
dictées plus rapide et fiable). En son absence, l'audio est envoyé en WAV brut.

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

Les modes *Propre* et *Prompt* nécessitent Ollama (ou le moteur IA cloud Groq). Les
prompts système de chaque mode sont définis dans `config.json` (`modes.*.system`) et
entièrement modifiables.

## Cloud (Groq) — optionnel mais rapide

Par défaut, tout tourne **en local** (faster-whisper + Ollama). Vous pouvez, au choix,
déléguer la transcription et/ou la correction IA à [**Groq**](https://groq.com), bien
plus rapide : la dictée est prête en **~1-2 s** au lieu de dizaines de secondes en local.

- **Transcription** — `transcribe_backend: "groq"` utilise `whisper-large-v3`, plus
  fidèle que `-turbo` en français (turbo saute parfois une phrase). Whisper ne reçoit
  **pas** de prompt par défaut : mesuré sur de vraies dictées, un prompt de vocabulaire
  lui fait sauter des phrases entières (`whisper_prompt: true` pour le réactiver).
- **Transcription Mistral** — `transcribe_backend: "mistral"` utilise Voxtral
  (`voxtral-mini-latest`), meilleurs scores publiés en français ; clé gratuite sur
  [console.mistral.ai](https://console.mistral.ai) (`mistral_api_key`). Repli sur Groq puis local.
- **Correction IA** — `llm_backend: "groq"` utilise `qwen/qwen3.8-27b`, qui corrige sans
  reformuler ; il reçoit ton `vocabulary`, ce qui corrige « groc » → « Groq ».
  Si un modèle est retiré par Groq, les suivants de la chaîne sont essayés
  (`openai/gpt-oss-120b`, puis Ollama), et **un échec est toujours notifié**.

Dans les deux cas, un échec du cloud (clé absente, réseau coupé, erreur serveur)
provoque un **repli automatique** et silencieux vers le moteur local — vous obtenez
toujours votre texte. Avant l'envoi, l'audio est compressé en Opus via `ffmpeg`
(uploads ~15× plus légers) et les erreurs réseau transitoires sont réessayées.

**Obtenir et configurer la clé :**

1. Créez une clé gratuite sur [console.groq.com/keys](https://console.groq.com/keys).
2. Renseignez-la dans **Réglages** de l'app, ou directement dans
   `~/.local/share/whisper-dictation/config.json` (champ `groq_api_key`).

> **Confidentialité** — la clé reste **locale** (dans `config.json`, jamais versionnée).
> L'indice de contexte tiré du presse-papier n'est utilisé qu'**en local** : il n'est
> **jamais** envoyé à Groq. Tant que les *backends* restent à `local`, rien ne sort de
> votre machine.

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

### Clés principales de `config.json`

| Clé                  | Rôle                                                                                          |
|----------------------|-----------------------------------------------------------------------------------------------|
| `model`              | Modèle faster-whisper local (`base` / `small` / `medium`…).                                   |
| `language`           | Langue de la dictée (`fr` par défaut).                                                         |
| `transcribe_backend` | Moteur de transcription : `local` (faster-whisper), `groq` ou `mistral` (cloud).              |
| `llm_backend`        | Moteur de correction IA : `ollama` (local) ou `groq` (cloud).                                  |
| `ollama_model`       | Modèle Ollama pour les modes *Propre* / *Prompt* (`qwen3.5:4b` par défaut).                    |
| `groq_api_key`       | Clé API Groq (vide par défaut ; saisie localement, **jamais versionnée**).                     |
| `groq_model`         | Modèle de transcription Groq (`whisper-large-v3`).                                             |
| `groq_llm_model`     | Modèle de correction IA Groq (`qwen/qwen3.8-27b`).                                             |
| `mistral_api_key`    | Clé API Mistral (Voxtral), optionnelle.                                                        |
| `whisper_prompt`     | `true` : envoie le vocabulaire en prompt à Whisper cloud (déconseillé, voir plus haut).        |
| `beam_size`          | Largeur de faisceau de la transcription locale ; `1` (défaut) = le plus rapide.               |
| `context_injection`  | `true` : guide la transcription **locale** avec un indice de contexte (presse-papier / fenêtre). |
| `vocabulary`         | Jargon : *hotwords* en local, `context_bias` chez Mistral, et consigne de l'IA de correction.  |
| `replacements`       | Table de remplacements littéraux appliqués après transcription (ex. `k8s` → `Kubernetes`).    |

`vocabulary` et `replacements` sont entièrement modifiables : ajoutez-y vos propres
termes techniques et corrections récurrentes.

## Journal de performance

Chaque dictée écrit une ligne dans
`~/.local/share/whisper-dictation/timings.log`, pratique pour repérer ce qui ralentit
(transcription locale lente, modèle IA lourd…) et comparer local vs cloud :

```
2026-06-24 14:32:10 | parle=8s texte=142c | transcription=1.4s ia=0.9s total=2.3s | moteur=groq/whisper-large-v3-turbo beam=1 ia=groq | vitesse=0.29x_du_temps_de_parole
```

On y lit, pour chaque dictée : l'horodatage, la durée de parole (`parle`), la
longueur du texte (`texte`), le temps de transcription, le temps d'IA et le temps
total, le **moteur réellement utilisé** (après repli éventuel), `beam_size`, le
backend IA, et le ratio `vitesse` (total / temps de parole : plus c'est bas, mieux
c'est).

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
