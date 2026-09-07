#!/usr/bin/env python3
"""Aligne plusieurs passes de transcription sur une grille temporelle commune.

Produit un fichier lisible ou, pour chaque fenetre de N secondes, on lit
cote a cote ce que chaque passe a entendu. Sert d'entree a la relecture.
"""
import json, sys, os, argparse

def charger(chemin):
    with open(chemin) as f:
        d = json.load(f)
    return d.get("segments", [])

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fenetre", type=int, default=20)
    p.add_argument("--out", required=True)
    p.add_argument("passes", nargs="+", help="etiquette=fichier.json")
    a = p.parse_args()

    passes = []
    for spec in a.passes:
        etiq, _, chemins = spec.partition("=")
        segs = []
        for c in chemins.split(","):
            segs += charger(c)
        segs.sort(key=lambda s: s["start"])
        passes.append((etiq, segs))

    fin = max(s["end"] for _, segs in passes for s in segs)
    n = int(fin // a.fenetre) + 1

    with open(a.out, "w") as f:
        for i in range(n):
            t0, t1 = i * a.fenetre, (i + 1) * a.fenetre
            bloc = []
            for etiq, segs in passes:
                txt = " ".join(s["text"].strip() for s in segs
                               if s["start"] < t1 and s["end"] > t0)
                bloc.append(f"  {etiq}: {txt}")
            if not any(l.split(":", 1)[1].strip() for l in bloc):
                continue
            f.write(f"### {t0//60:02d}:{t0%60:02d} - {t1//60:02d}:{t1%60:02d}\n")
            f.write("\n".join(bloc) + "\n\n")
    print(f"{a.out} : {n} fenetres de {a.fenetre}s")

if __name__ == "__main__":
    main()
