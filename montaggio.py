"""Inquadrature del film: timecode, EDL CMX3600, AAF, tagli automatici.

Dall'EDL o dall'AAF si ricavano i tagli della traccia video e il nome della
clip sorgente di ogni inquadratura; senza montaggio i tagli vengono rilevati
da ffmpeg. Qui si leggono anche durata, cadenza e timecode del video e le
eventuali bande nere da togliere prima dell'analisi.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------
# Timecode
# --------------------------------------------------------------------------

def tc_a_secondi(tc: str, fps: float) -> float:
    """Converte HH:MM:SS:FF (o HH:MM:SS;FF drop frame) in secondi."""
    drop = ";" in tc
    h, m, s, f = (int(x) for x in re.split(r"[:;.]", tc))
    base = round(fps)
    frame = ((h * 60 + m) * 60 + s) * base + f
    if drop and base in (30, 60):
        perdi = 2 if base == 30 else 4
        minuti = h * 60 + m
        frame -= perdi * (minuti - minuti // 10)
    return frame / fps


def secondi_a_tc(sec: float, fps: float) -> str:
    base = round(fps)
    frame = round(sec * fps)
    f = frame % base
    s = frame // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


# --------------------------------------------------------------------------
# Inquadrature
# --------------------------------------------------------------------------

@dataclass
class Inquadratura:
    numero: int
    inizio: float          # secondi nel file video
    fine: float
    tc_in: str             # timecode della sequenza
    tc_out: str
    clip: str = ""
    meta: dict = field(default_factory=dict)   # dal bin dell'AAF: scena, inquadratura, ciak, camera...

    def scena(self) -> str:
        """Scena/inquadratura/ciak dal bin, oppure il nome della clip."""
        ciak = "/".join(self.meta.get(k, "") for k in ("scena", "inquadratura", "ciak") if self.meta.get(k))
        return ciak or self.clip


RIGA_EVENTO = re.compile(
    r"^(\d+)\s+(\S+)\s+(\S+)\s+(C|D|W\d*|K\s*B?|K\s*O?)\s+(\d+\s+)?"
    r"(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+"
    r"(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)"
)


def leggi_edl(percorso: Path) -> list:
    """Eventi {tc_in, tc_out, clip, video} da un EDL CMX3600."""
    tutti, corrente = [], None
    for riga in percorso.read_text(encoding="utf-8", errors="replace").splitlines():
        m = RIGA_EVENTO.match(riga.strip())
        if m:
            traccia = m.group(3).upper()
            corrente = {"tc_in": m.group(8), "tc_out": m.group(9), "clip": m.group(2),
                        "video": "V" in traccia or traccia == "B"}
            tutti.append(corrente)
            continue
        nome = re.match(r"^\*\s*FROM CLIP NAME:\s*(.+)$", riga.strip(), re.I)
        if nome and corrente:
            corrente["clip"] = nome.group(1).strip()
    return tutti


def leggi_montaggio(percorso: Path, fps: float, tc_inizio_video: float) -> list:
    """Inquadrature da un EDL o da un AAF, secondo l'estensione."""
    if percorso.suffix.lower() == ".aaf":
        import aaf
        dati = aaf.leggi(percorso)
        print(f"AAF: sequenza «{dati['sequenza']['nome']}», inizia a {dati['sequenza']['inizio']}")
        eventi = dati["eventi"]
    else:
        eventi = leggi_edl(percorso)
    return inquadrature_da_eventi(eventi, fps, tc_inizio_video)


def inquadrature_da_eventi(tutti: list, fps: float, tc_inizio_video: float) -> list:
    """Eventi di montaggio -> inquadrature video in ordine, senza sovrapposizioni."""
    eventi = sorted((e for e in tutti if e["video"]), key=lambda e: tc_a_secondi(e["tc_in"], fps))
    inquadrature = []
    for e in eventi:
        inizio = tc_a_secondi(e["tc_in"], fps) - tc_inizio_video
        fine = tc_a_secondi(e["tc_out"], fps) - tc_inizio_video
        # Le dissolvenze producono eventi sovrapposti: tengo solo il tratto nuovo.
        if inquadrature and inizio < inquadrature[-1].fine:
            inquadrature[-1].fine = inizio
        if fine - inizio < 1 / fps:
            continue
        inquadrature.append(Inquadratura(0, inizio, fine, e["tc_in"], e["tc_out"], e["clip"],
                                         dict(e.get("meta") or {})))
    inquadrature = [q for q in inquadrature if q.fine - q.inizio >= 1 / fps]
    for i, q in enumerate(inquadrature, 1):
        q.numero = i
    return inquadrature


def rileva_tagli(video: Path, durata: float, fps: float, tc_inizio_video: float,
                 soglia: float = 0.3) -> list:
    """Rileva i tagli con il filtro scene di ffmpeg (quando manca il montaggio)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
           "-vf", f"scale=320:-2,select='gt(scene,{soglia})',showinfo", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    tagli = [float(t) for t in re.findall(r"pts_time:([\d.]+)", out)]
    confini = [0.0] + [t for t in tagli if 0.2 < t < durata - 0.2] + [durata]
    return [Inquadratura(i + 1, a, b, secondi_a_tc(a + tc_inizio_video, fps),
                         secondi_a_tc(b + tc_inizio_video, fps))
            for i, (a, b) in enumerate(zip(confini, confini[1:]))]


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------

def info_video(video: Path) -> tuple:
    """(fps, durata in secondi, timecode di inizio o None)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate:stream_tags=timecode:format=duration:format_tags=timecode",
         "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout
    dati = json.loads(out)
    flusso = dati["streams"][0]
    num, den = flusso["r_frame_rate"].split("/")
    tc = flusso.get("tags", {}).get("timecode") or dati["format"].get("tags", {}).get("timecode")
    return float(num) / float(den), float(dati["format"]["duration"]), tc


def rileva_bande_nere(video: Path, durata: float):
    """Filtro crop che toglie le bande nere (letterbox), o None se non ce ne sono."""
    aree = []
    for frazione in (0.2, 0.4, 0.6, 0.8):
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", f"{durata * frazione:.1f}", "-t", "4", "-i", str(video),
             "-vf", "cropdetect=24:2:0", "-an", "-f", "null", "-"],
            capture_output=True, text=True).stderr
        trovati = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out)
        if trovati:
            aree.append(tuple(int(x) for x in trovati[-1]))
    if not aree:
        return None
    # Il ritaglio più ampio tra i campioni: meglio una banda residua che tagliare l'immagine.
    w, h, x, y = max(aree, key=lambda a: a[0] * a[1])
    pieno = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True).stdout.strip().split(",")
    if [str(w), str(h)] == pieno[:2] or h < 100:
        return None
    return f"crop={w}:{h}:{x}:{y}"
