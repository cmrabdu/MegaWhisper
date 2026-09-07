# Transcription d'un fichier existant, en plusieurs passes

L'app `MegaWhisper` transcrit ce qu'on dicte au micro. Ces deux scripts servent l'autre
cas : un fichier audio déjà enregistré (dictaphone, réunion, note vocale longue) qu'on
veut transcrire aussi fidèlement que possible, quitte à y passer plusieurs minutes.

Le principe : Whisper se trompe, mais il ne se trompe pas de la même façon selon le
modèle, le découpage et le prompt. On lance donc plusieurs passes indépendantes sur le
même audio, on les aligne sur une grille temporelle, et on arbitre là où elles divergent.

## Préparer l'audio

Groq refuse les fichiers trop lourds. Un MP3 stéréo de 14 min pèse 20 Mo, un FLAC 16 kHz
mono en pèse 30 : trop. Passer par Opus.

```sh
ffmpeg -i note.MP3 -ac 1 -ar 16000 -c:a libopus -b:a 64k plein.opus
```

Pour la passe par tronçons, découper avec un recouvrement (ici 300 s de tronçon toutes
les 285 s, donc 15 s de recouvrement) :

```sh
for i in 0 1 2; do
  ffmpeg -ss $((i*285)) -t 300 -i note.MP3 -ac 1 -ar 16000 -c:a flac tronc$i.flac
done
```

## Les passes

```sh
# 1. le modele complet, fichier entier
tools/transcrire-fichier.py --audio plein.opus --model whisper-large-v3 --name A --out passes

# 2. le modele turbo : segmentation differente, donc erreurs differentes
tools/transcrire-fichier.py --audio plein.opus --model whisper-large-v3-turbo --name B --out passes

# 3. par troncons : le contexte change, les fins de phrase aussi
for i in 0 1 2; do
  tools/transcrire-fichier.py --audio tronc$i.flac --model whisper-large-v3 \
      --name C$i --out passes --offset $((i*285))
done

# 4. avec le vocabulaire du sujet en prompt
tools/transcrire-fichier.py --audio plein.opus --model whisper-large-v3 --name D --out passes \
  --prompt "Dictee en francais sur un projet de drone : pixel lock, Jetson, Betaflight, MSP, pitch, yaw, roll, throttle."
```

`--offset` recale les horodatages d'un tronçon sur l'audio complet : sans lui, les
passes ne s'alignent pas.

Chaque passe écrit `<nom>.json` (verbose_json complet, segments horodatés) et
`<nom>.txt` (un segment par ligne, horodaté).

## L'alignement

```sh
tools/aligner-passes.py --fenetre 20 --out passes/aligne.txt \
  A=passes/A.json B=passes/B.json C=passes/C0.json,passes/C1.json,passes/C2.json D=passes/D.json
```

On obtient, fenêtre de 20 s par fenêtre de 20 s, ce que chaque passe a entendu. Les
divergences sautent aux yeux. Deux pièges à connaître en relisant ce fichier : les
fenêtres se recouvrent (un segment à cheval apparaît deux fois) et les tronçons de la
passe C répètent du texte à leurs jointures. Le texte final doit être dédupliqué.

## Arbitrer

`large-v3` est le plus fiable, `turbo` le moins. Quand les passes divergent, c'est le
sens qui tranche, pas le vote : une lecture majoritaire qui n'est pas du français perd
contre une lecture minoritaire qui l'est.

Pour un passage qui résiste, réécouter court et ralenti. Un extrait de 6 s, à vitesse
normale puis à `atempo=0.6`, sur les deux modèles, sépare la plupart des cas :

```sh
ffmpeg -ss 770 -t 7 -i note.MP3 -ac 1 -ar 16000 -af "atempo=0.6" -c:a flac zoom.flac
```

Ce qui reste indécidable après ça se marque dans le texte et se justifie en fin de
document, avec ce que chaque passe a entendu. Une transcription qui cache ses doutes
vaut moins qu'une transcription qui les montre.

## La clé

Les deux scripts lisent `groq_api_key` dans
`~/.local/share/whisper-dictation/config.json`, comme le reste de MegaWhisper.
