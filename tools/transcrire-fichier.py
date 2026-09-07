#!/usr/bin/env python3
"""Transcription multi-passes d'un fichier audio via l'API Groq (Whisper).

Chaque passe ecrit deux fichiers dans --out :
  <nom>.json  reponse verbose_json complete (segments horodates)
  <nom>.txt   texte seul, un segment par ligne avec son horodatage

Usage :
  transcrire.py --audio a.flac --model whisper-large-v3 --name passeA --out DIR
"""
import argparse, json, mimetypes, os, ssl, sys, time, urllib.error, urllib.request, uuid

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CFG = os.path.expanduser("~/.local/share/whisper-dictation/config.json")


def cle():
    with open(CFG) as f:
        k = json.load(f).get("groq_api_key", "").strip()
    if not k:
        sys.exit("cle API Groq absente de config.json")
    return k


def multipart(chemin, champs):
    limite = "----megawhisper" + uuid.uuid4().hex
    corps = b""
    for nom, val in champs.items():
        if val is None:
            continue
        corps += (f"--{limite}\r\nContent-Disposition: form-data; name=\"{nom}\"\r\n\r\n{val}\r\n").encode()
    mime = mimetypes.guess_type(chemin)[0] or "application/octet-stream"
    with open(chemin, "rb") as f:
        donnees = f.read()
    corps += (f"--{limite}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"{os.path.basename(chemin)}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
    corps += donnees + b"\r\n" + f"--{limite}--\r\n".encode()
    return corps, f"multipart/form-data; boundary={limite}"


def transcrire(chemin, modele, langue, temperature, prompt, essais=4):
    champs = {"model": modele, "response_format": "verbose_json",
              "temperature": str(temperature)}
    if langue:
        champs["language"] = langue
    if prompt:
        champs["prompt"] = prompt
    corps, ctype = multipart(chemin, champs)
    entetes = {"Authorization": f"Bearer {cle()}", "Content-Type": ctype,
               "User-Agent": "megawhisper/1.0", "Accept": "application/json"}
    dernier = None
    for n in range(essais):
        try:
            req = urllib.request.Request(URL, data=corps, headers=entetes, method="POST")
            with urllib.request.urlopen(req, timeout=900, context=ssl.create_default_context()) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            dernier = f"HTTP {e.code}: {detail}"
            if e.code not in (408, 429, 500, 502, 503, 504):
                break
        except Exception as e:  # reseau
            dernier = repr(e)
        time.sleep(3 * (n + 1))
    sys.exit(f"echec transcription {chemin} / {modele} : {dernier}")


def hms(s):
    return f"{int(s)//60:02d}:{int(s)%60:02d}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio", required=True)
    p.add_argument("--model", default="whisper-large-v3")
    p.add_argument("--name", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--language", default="fr")
    p.add_argument("--temperature", default="0")
    p.add_argument("--prompt", default="")
    p.add_argument("--offset", type=float, default=0.0, help="decalage a ajouter aux horodatages")
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    rep = transcrire(a.audio, a.model, a.language, a.temperature, a.prompt)
    dt = time.time() - t0

    segs = rep.get("segments", [])
    for s in segs:
        s["start"] = s.get("start", 0) + a.offset
        s["end"] = s.get("end", 0) + a.offset

    with open(os.path.join(a.out, a.name + ".json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, a.name + ".txt"), "w") as f:
        for s in segs:
            f.write(f"[{hms(s['start'])}] {s['text'].strip()}\n")
        if not segs:
            f.write(rep.get("text", "").strip() + "\n")

    print(f"{a.name}: {a.model} sur {os.path.basename(a.audio)} — "
          f"{len(segs)} segments, {len(rep.get('text',''))} caracteres, {dt:.0f}s")


if __name__ == "__main__":
    main()
