#!/usr/bin/env python3
"""Génère les sons de l'interface (start / stop / done).

Timbre « verre / marimba » doux : partiels sinusoïdaux légèrement inharmoniques,
attaque adoucie, décroissance exponentielle, léger filtre passe-bas et petite
réverbération stéréo. Relancer :  python3 sounds/generate.py
"""
import wave
from pathlib import Path

import numpy as np

SR = 48000
ICI = Path(__file__).parent

# Fréquences (Hz)
G5, C6 = 783.99, 1046.50

# Partiels du timbre : (rapport de fréquence, amplitude, facteur de décroissance)
# Le 3,93 donne le « tic » de mailloche qui s'éteint très vite.
PARTIELS = [(1.0, 1.0, 1.0), (2.0, 0.16, 1.8), (3.93, 0.05, 5.0), (0.5, 0.10, 0.7)]


def note(freq, duree, tau, attaque=0.005):
    """Une note : somme de partiels, attaque en cosinus, décroissance exponentielle."""
    t = np.arange(int(duree * SR)) / SR
    s = np.zeros_like(t)
    for rapport, amp, vitesse in PARTIELS:
        # léger désaccord pour un son moins « synthétique »
        f = freq * rapport * (1 + 0.0007 * (rapport - 1))
        s += amp * np.sin(2 * np.pi * f * t) * np.exp(-t * vitesse / tau)
    n = int(attaque * SR)
    s[:n] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n))
    return s


def passe_bas(x, fc):
    """Filtre passe-bas à un pôle (appliqué deux fois pour une pente plus douce)."""
    a = np.exp(-2 * np.pi * fc / SR)
    for _ in range(2):
        y = np.empty_like(x)
        acc = 0.0
        for i, v in enumerate(x):
            acc = (1 - a) * v + a * acc
            y[i] = acc
        x = y
    return x


def reverb(x, delais, gain=0.22):
    """Quelques échos atténués et adoucis : une petite « pièce »."""
    y = x.copy()
    for k, d in enumerate(delais):
        n = int(d * SR)
        echo = passe_bas(x, 3500 - 600 * k) * gain * 0.6 ** k
        y[n:] += echo[: len(y) - n]
    return y


def son(notes, duree, fondu=0.04, crete=-6):
    """notes = [(début_s, fréquence, tau, volume)] -> tableau stéréo normalisé."""
    mono = np.zeros(int(duree * SR))
    for debut, f, tau, vol in notes:
        i = int(debut * SR)
        mono[i:] += vol * note(f, duree - debut, tau)
    mono = passe_bas(mono, 7000)
    # Stéréo : réverbs aux délais légèrement différents à gauche et à droite
    g = reverb(mono, [0.023, 0.041, 0.067])
    d = reverb(mono, [0.029, 0.047, 0.059])
    st = np.stack([g, d], axis=1)
    # Fondu de sortie pour éviter tout clic
    n = int(fondu * SR)
    st[-n:] *= np.linspace(1, 0, n)[:, None] ** 2
    return st / np.abs(st).max() * 10 ** (crete / 20)


def ecrire(nom, st):
    donnees = (st * 32767).astype("<i2")
    with wave.open(str(ICI / nom), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(donnees.tobytes())


SONS = {
    # Début : quarte montante sol → do, brève et engageante
    "start.wav": son([(0.0, G5, 0.07, 0.8), (0.065, C6, 0.08, 1.0)], 0.24),
    # Fin d'enregistrement : le miroir descendant do → sol
    "stop.wav": son([(0.0, C6, 0.06, 0.9), (0.06, G5, 0.07, 1.0)], 0.20),
    # Texte prêt : une seule note grave et discrète, juste « c'est bon »
    "done.wav": son([(0.0, G5 / 2, 0.10, 1.0)], 0.32, fondu=0.10, crete=-10),
}

if __name__ == "__main__":
    for nom, st in SONS.items():
        ecrire(nom, st)
        rms = np.sqrt((st ** 2).mean())
        spectre = np.abs(np.fft.rfft(st[:, 0])) ** 2
        freqs = np.fft.rfftfreq(len(st), 1 / SR)
        aigus = spectre[freqs > 8000].sum() / spectre.sum()
        print(f"{nom:10s} {len(st) / SR * 1000:4.0f} ms  crête {20 * np.log10(np.abs(st).max()):5.1f} dBFS"
              f"  RMS {20 * np.log10(rms):5.1f} dBFS  énergie >8 kHz {aigus:.1e}"
              f"  bords {abs(st[0]).max():.4f}/{abs(st[-1]).max():.4f}")
