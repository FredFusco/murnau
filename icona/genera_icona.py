#!/usr/bin/env python3
"""Disegna l'icona di Murnau: un ventaglio di artigli a falce, avorio su nero, con un artiglio rosso staccato.

Scrive icona_murnau.svg accanto a questo file. Le misure sono in un quadrato di 200 unità.
"""

import math
from pathlib import Path

NERO, AVORIO, ROSSO = "#121114", "#f2ede2", "#d22a1f"


def falce(x0, y0, partenza, angolo, lunghezza, larghezza, curva, sale=.7, punti=200) -> tuple:
    """Lama a falce lungo una linea centrale curva, con lo spessore che nasce da zero e si affila in punta.

    La linea parte da (x0, y0) a metà strada tra la direzione `partenza` (in gradi, -90 = su), comune a
    tutte le lame, e la direzione della punta: vicino all'origine le lame quasi coincidono e la mano
    finisce in un'unica coda morbida, senza spigoli e senza curve a S.
    Poi la lama piega verso la sua punta, che sta nella direzione `angolo` a distanza `lunghezza`.
    curva > 0 incurva ulteriormente la lama in senso antiorario. `sale` regola quanto in fretta la lama
    si allarga dopo la coda (più alto = coda più sottile).
    """
    a, d = math.radians(angolo), math.radians((partenza + angolo) / 2)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = uy, -ux
    x3, y3 = x0 + ux * lunghezza, y0 + uy * lunghezza
    x1, y1 = x0 + math.cos(d) * lunghezza * .22, y0 + math.sin(d) * lunghezza * .22
    x2, y2 = x0 + ux * lunghezza * .66 + nx * curva * 1.6, y0 + uy * lunghezza * .66 + ny * curva * 1.6

    def centro(t):
        s = 1 - t
        return (s ** 3 * x0 + 3 * s * s * t * x1 + 3 * s * t * t * x2 + t ** 3 * x3,
                s ** 3 * y0 + 3 * s * s * t * y1 + 3 * s * t * t * y2 + t ** 3 * y3)

    def normale(t):
        s = 1 - t
        dx = 3 * s * s * (x1 - x0) + 6 * s * t * (x2 - x1) + 3 * t * t * (x3 - x2)
        dy = 3 * s * s * (y1 - y0) + 6 * s * t * (y2 - y1) + 3 * t * t * (y3 - y2)
        l = math.hypot(dx, dy)
        return dy / l, -dx / l

    scende = 1.4
    picco = sale / (sale + scende)
    norma = 1 / (picco ** sale * (1 - picco) ** scende)

    def mezzo_spessore(t):
        return larghezza / 2 * norma * t ** sale * (1 - t) ** scende

    sopra, sotto = [], []
    for k in range(punti + 1):
        tt = k / punti
        (px, py), (qx, qy), m = centro(tt), normale(tt), mezzo_spessore(tt)
        sopra.append((px + qx * m, py + qy * m))
        sotto.append((px - qx * m, py - qy * m))
    return tracciato(sopra, sotto), sopra, sotto


def tracciato(sopra, sotto) -> str:
    f = lambda q: f"{q[0]:.2f} {q[1]:.2f}"
    return "M " + " L ".join(f(q) for q in sopra) + " L " + " L ".join(f(q) for q in reversed(sotto)) + " Z"


def icona() -> str:
    # Tutte le lame partono dallo stesso punto e quasi nella stessa direzione: un'unica coda morbida.
    cx, cy, stelo = 40, 170, -36
    chiare = [falce(cx, cy, stelo, -63, 156, 15, 12)[0],
              falce(cx, cy, stelo, -42, 154, 15, 12)[0],
              falce(cx, cy, stelo, -22, 148, 14, 10)[0]]
    # L'artiglio rosso esce da sotto le lame chiare: le separa solo il loro sottile bordo nero.
    rosso = falce(cx, cy, stelo, -5, 144, 16, 8)[0]
    bordino = 2.2
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="1024" height="1024">
  <defs><clipPath id="tondo"><rect x="10" y="10" width="180" height="180" rx="41"/></clipPath></defs>
  <g clip-path="url(#tondo)">
    <rect width="200" height="200" fill="{NERO}"/>
    <path d="{rosso}" fill="{ROSSO}"/>
    <!-- bordino: un alone nero sotto le lame chiare, visibile solo dove coprono il rosso -->
    <g fill="{NERO}" stroke="{NERO}" stroke-width="{bordino * 2}" stroke-linejoin="round">{''.join(f'<path d="{d}"/>' for d in chiare)}</g>
    <g fill="{AVORIO}">{''.join(f'<path d="{d}"/>' for d in chiare)}</g>
  </g>
</svg>
"""


if __name__ == "__main__":
    (Path(__file__).parent / "icona_murnau.svg").write_text(icona(), encoding="utf-8")
