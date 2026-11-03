#!/usr/bin/env python3
"""Murnau: cerca gli errori di ripresa in un film montato.

Analizza lo scarico di un film (con l'EDL o l'AAF della sequenza, o rilevando i
tagli da solo) e segnala, inquadratura per inquadratura:
  - troupe o attrezzatura in campo (boom, macchina da presa, stativi, operatori)
  - troupe nei riflessi (specchi, vetri, occhiali, carrozzerie)
  - attori e comparse che guardano in macchina
  - microfoni lavalier visibili
  - prese elettriche e cavi

Tutto avviene su questo computer: OWLv2 e YuNet trovano i sospetti, Qwen3-VL
(tramite Ollama) esamina i fotogrammi e Gemma 4 verifica ogni sospetto su un
ritaglio ingrandito.

    python murnau.py FILM.mov --montaggio FILM.aaf --out risultati

Le fasi, in ordine: inquadrature, fotogrammi, oggetti, volti, esame, verifica. Ogni
risultato viene salvato appena calcolato (archivi.Archivio): rilanciando un'analisi
interrotta si riprende da dove era arrivata.
"""

import argparse
import atexit
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import archivi
import coordinamento
import esporta
import montaggio
import progresso
import rilevatori
from archivi import Archivio
from progresso import avanzamento, avviso, elenco, fase, piano, quanti
from rilevatori import CATEGORIE, CATEGORIE_OGGETTI

LARGHEZZA = 1280           # pixel dei fotogrammi analizzati
MAX_VERIFICHE = 3          # sospetti verificati al massimo per inquadratura e categoria


class Analisi:
    """Tutto ciò che le fasi si passano: opzioni, inquadrature, fotogrammi, risultati già calcolati."""

    def __init__(self, args):
        self.args = args
        self.video: Path = args.video
        self.modello: str = args.modello                    # esame dei fotogrammi
        self.modello_verifica: str = args.modello_verifica  # verifica dei sospetti
        self.seconda_opinione: str = args.seconda_opinione  # ricontrolla ciò che la verifica accetta ("" = no)
        self.categorie = {c.strip() for c in args.categorie.split(",") if c.strip()}
        self.out: Path = args.out
        self.lavoro = self.out / "lavoro"
        self.fps = self.durata = self.tc_inizio_s = 0.0
        self.tc_inizio = ""
        self.inquadrature = []
        self.ritaglio = ""         # filtro di ffmpeg che toglie le bande nere, se ci sono
        self.fotogrammi = {}       # numero inquadratura -> [(secondi, percorso)]
        self.chiave_cat = ",".join(sorted(self.categorie))
        self.illeggibili = set()   # fotogrammi che non si sono potuti leggere
        self.non_esaminati = []
        self.non_verificate = set()
        self.ffmpeg = None         # l'estrazione dei fotogrammi in corso, da fermare se l'analisi si interrompe
        self.finita = False

    @property
    def con_oggetti(self) -> bool:
        return bool(self.categorie & CATEGORIE_OGGETTI)

    def tutti_i_fotogrammi(self) -> list:
        return [p for q in self.inquadrature for _, p in self.fotogrammi[q.numero]]

    def archivi(self) -> None:
        # I risultati dipendono dal modello e dalle domande: con un altro modello o domande nuove si
        # riparte da file nuovi, e quelli vecchi restano dove sono.
        v = rilevatori.VERSIONE_DOMANDE
        self.oggetti = Archivio(self.lavoro / f"oggetti_v{v}.jsonl")
        self.volti = Archivio(self.lavoro / f"sguardi_v{rilevatori.VERSIONE_SGUARDI}.jsonl")
        self.esame = Archivio(self.lavoro / f"esame_{_nome_file(self.modello)}_v{v}.jsonl")
        self.verifiche = Archivio(self.lavoro / f"verifiche_{_nome_file(self.modello_verifica)}_v{v}.jsonl")
        self.seconde = Archivio(self.lavoro / f"seconde_{_nome_file(self.seconda_opinione or 'nessuna')}_v{v}.jsonl")


def _nome_file(modello: str) -> str:
    return re.sub(r"[^\w.-]", "_", modello)


_ULTIMO_TURNO = [0.0]


def turno() -> None:
    """Prima di usare il modello: se Vertov sta analizzando, gli si lascia il modello.
    Si controlla al più ogni 30 secondi: costa poco ma non serve farlo a ogni fotogramma."""
    if time.time() - _ULTIMO_TURNO[0] > 30:
        coordinamento.aspetta_il_turno(progresso.attesa)
        _ULTIMO_TURNO[0] = time.time()


def tieni_sveglio() -> None:
    """Il Mac non va in stop finché dura l'analisi (lo schermo invece può spegnersi): un'analisi lanciata
    la sera non si ferma a metà. caffeinate esce da solo quando l'analisi finisce, in qualunque modo."""
    try:
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass  # senza caffeinate l'analisi va avanti lo stesso


def chiusura(a: Analisi) -> None:
    """All'uscita, normale o interrotta: niente resta acceso e il modello non resta in memoria."""
    if a.ffmpeg and a.ffmpeg.poll() is None:
        a.ffmpeg.kill()
    if progresso.registro:
        progresso.registro.chiudi()
        if progresso.registro.dati["origine"] == "terminale":
            coordinamento.notifica(f"{'Analisi finita' if a.finita else 'Analisi interrotta'}: {a.video.name}")
    for modello in {a.modello, a.modello_verifica, a.seconda_opinione} - {""}:
        coordinamento.libera_modello(modello, rilevatori.CONTESTO)


# --------------------------------------------------------------------------
# 1. Inquadrature
# --------------------------------------------------------------------------

def prepara_inquadrature(a: Analisi) -> None:
    fase("preparazione")
    args = a.args
    a.fps, a.durata, tc_file = montaggio.info_video(a.video)
    a.tc_inizio = args.tc_inizio or tc_file or "00:00:00:00"
    a.tc_inizio_s = montaggio.tc_a_secondi(a.tc_inizio, a.fps)
    print(f"Video: {a.durata / 60:.1f} minuti a {a.fps:g} fps, inizia a {a.tc_inizio}")
    if args.montaggio:
        inquadrature = inquadrature_del_montaggio(a)
    else:
        print("Nessun EDL o AAF: rilevo i tagli con ffmpeg...")
        inquadrature = montaggio.rileva_tagli(a.video, a.durata, a.fps, a.tc_inizio_s, args.soglia_tagli)
    a.inquadrature = [q for q in inquadrature if q.numero >= args.da and (args.a is None or q.numero <= args.a)]
    if not a.inquadrature:
        sys.exit("Nessuna inquadratura da analizzare.")
    print(f"{len(a.inquadrature)} inquadrature da analizzare")
    a.ritaglio = montaggio.rileva_bande_nere(a.video, a.durata) or ""
    if a.ritaglio:
        print(f"Bande nere: analizzo solo l'immagine ({a.ritaglio})")
    # Prima stima del lavoro, dalle durate: diventa esatta dopo l'estrazione dei fotogrammi.
    stimati = [max(1, round((q.fine - q.inizio) / args.passo)) for q in a.inquadrature]
    esami = sum(len(scegli_per_esame([None] * n, q.fine - q.inizio, args.accurato))
                for q, n in zip(a.inquadrature, stimati))
    piano(fotogrammi=round(sum(q.fine - q.inizio for q in a.inquadrature)), **unita_dei_rilevatori(a, sum(stimati), sum(stimati)),
          modello=0 if rilevatori.modello_caricato(a.modello) else 1, esame=esami, verifica=2 * len(a.inquadrature))


def inquadrature_del_montaggio(a: Analisi) -> list:
    """Le inquadrature dell'EDL o dell'AAF, ristrette a ciò che c'è nel film (con un avviso per le altre)."""
    inquadrature = montaggio.leggi_montaggio(a.args.montaggio, a.fps, a.tc_inizio_s)
    fuori = [q for q in inquadrature if q.inizio < -0.5 or q.fine > a.durata + 0.5]
    if len(fuori) > len(inquadrature) / 2:
        sys.exit(f"Il montaggio non corrisponde al video: {len(fuori)} inquadrature su {len(inquadrature)} cadono "
                 f"fuori dal file. Controlla il timecode di inizio (ora {a.tc_inizio}).")
    saltate = [q for q in fuori if q.fine <= 0 or q.inizio >= a.durata]
    tagliate = [q for q in fuori if q not in saltate]
    for gruppo, uno, molti in (
            (saltate, "fuori dal film e non è stata controllata", "fuori dal film e non sono state controllate"),
            (tagliate, "in parte fuori dal film ed è stata controllata solo per il tratto che c'è",
             "in parte fuori dal film e sono state controllate solo per il tratto che c'è")):
        if gruppo:
            avviso((f"1 inquadratura del montaggio cade {uno}" if len(gruppo) == 1 else
                    f"{len(gruppo)} inquadrature del montaggio cadono {molti}") +
                   f". Verifica che film e montaggio siano della stessa versione (inizio del film: {a.tc_inizio}).")
    inquadrature = [q for q in inquadrature if q.fine > 0 and q.inizio < a.durata]
    for q in inquadrature:
        q.inizio, q.fine = max(0.0, q.inizio), min(a.durata, q.fine)
    return inquadrature


def unita_dei_rilevatori(a: Analisi, oggetti: int, volti: int) -> dict:
    """Le voci del piano per OWLv2 e per i volti, solo se servono."""
    return {**({"oggetti": oggetti} if a.con_oggetti else {}),
            **({"volti": volti} if "sguardi" in a.categorie else {})}


# --------------------------------------------------------------------------
# 2. Fotogrammi
# --------------------------------------------------------------------------

def filtri_ffmpeg(ritaglio: str, *altri: str) -> str:
    return ",".join(([ritaglio] if ritaglio else []) + list(altri))


def estrai_fotogrammi(a: Analisi) -> None:
    """Un fotogramma ogni `passo` secondi, in un solo passaggio sul film.

    In a.fotogrammi: {numero inquadratura: [(secondi, percorso)]}. I fotogrammi a ridosso
    dei tagli vengono scartati; le inquadrature più brevi del passo ricevono il loro
    fotogramma centrale.
    """
    fase("fotogrammi")
    passo, cartella = a.args.passo, a.lavoro / "fotogrammi"
    inizio = max(0.0, math.floor(min(q.inizio for q in a.inquadrature) / passo) * passo)
    fine = max(q.fine for q in a.inquadrature)
    filtri = filtri_ffmpeg(a.ritaglio, f"fps=1/{passo}", f"scale={LARGHEZZA}:-2")
    completo = cartella / ".completo"
    firma = json.dumps({"inizio": inizio, "fine": fine, "passo": passo, "filtri": filtri})
    if not completo.exists() or completo.read_text() != firma:
        shutil.rmtree(cartella, ignore_errors=True)
        cartella.mkdir(parents=True)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-ss", f"{inizio:.3f}", "-to", f"{fine:.3f}", "-i", str(a.video), "-an", "-sn",
               "-vf", filtri, "-q:v", "3", "-start_number", "0", str(cartella / "f%06d.jpg")]
        proc = a.ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        totale = round(fine - inizio)
        for riga in proc.stdout:
            m = re.match(r"out_time_us=(\d+)", riga)
            if m:
                avanzamento("fotogrammi", min(totale, int(m.group(1)) // 1_000_000), totale)
        if proc.wait() != 0:
            sys.exit("ffmpeg non è riuscito a leggere il video.")
        completo.write_text(firma)

    margine = 1.5 / a.fps
    a.fotogrammi = {q.numero: [] for q in a.inquadrature}
    tempi = sorted((inizio + int(p.stem[1:]) * passo, p) for p in cartella.glob("f*.jpg"))
    i = 0
    for q in a.inquadrature:
        while i < len(tempi) and tempi[i][0] < q.inizio + margine:
            i += 1
        j = i
        while j < len(tempi) and tempi[j][0] <= q.fine - margine:
            a.fotogrammi[q.numero].append(tempi[j])
            j += 1
        if not a.fotogrammi[q.numero]:
            t = (q.inizio + q.fine) / 2
            dest = cartella / f"q{q.numero:05d}.jpg"
            if not dest.exists():
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(a.video),
                                "-frames:v", "1", "-vf", filtri_ffmpeg(a.ritaglio, f"scale={LARGHEZZA}:-2"),
                                "-q:v", "3", "-y", str(dest)])
            if dest.exists():
                a.fotogrammi[q.numero].append((t, dest))
    print(f"{len(a.tutti_i_fotogrammi())} fotogrammi estratti")


def scegli_per_esame(fotogrammi: list, durata: float, accurato: bool) -> list:
    """Fotogrammi da far esaminare a Qwen3-VL, distribuiti nell'inquadratura."""
    if accurato:
        n = min(12, max(1, math.ceil(durata / 2)))
    else:
        n = 1 if durata < 4 else 2 if durata < 10 else 3 if durata < 20 else 4
    n = min(n, len(fotogrammi))
    if n <= 0:
        return []
    if n == 1:
        return [fotogrammi[len(fotogrammi) // 2]]
    passo = (len(fotogrammi) - 1) / (n - 1)
    return [fotogrammi[round(k * passo)] for k in range(n)]


def da_esaminare(a: Analisi) -> list:
    """I fotogrammi che Qwen3-VL deve ancora esaminare (quelli già esaminati sono nell'archivio)."""
    return [p for q in a.inquadrature
            for _, p in scegli_per_esame(a.fotogrammi[q.numero], q.fine - q.inizio, a.args.accurato)
            if f"{p.name}|{a.chiave_cat}" not in a.esame]


# --------------------------------------------------------------------------
# 3-4. Oggetti (OWLv2) e volti
# --------------------------------------------------------------------------

def rileva(a: Analisi, nome: str, archivio: Archivio, crea, metodo: str, libera: bool = False) -> None:
    """Passa ogni fotogramma non ancora visto a un rilevatore e ne archivia i risultati."""
    da_fare = [p for p in a.tutti_i_fotogrammi() if p.name not in archivio]
    fase(nome)
    if not da_fare:
        return
    rilevatore = crea()
    cerca = getattr(rilevatore, metodo)
    for n, p in enumerate(da_fare, 1):
        try:
            archivio.metti(p.name, cerca(p))
        except OSError as e:
            print(f"{p.name}: {e}")
            a.illeggibili.add(p.name)  # non archiviato: rilanciando si riprova
        avanzamento(nome, n, len(da_fare))
    if libera:
        rilevatore.libera()


def rileva_oggetti(a: Analisi) -> None:
    if a.con_oggetti:
        print("Carico il rilevatore di oggetti (OWLv2)...")
        rileva(a, "oggetti", a.oggetti, lambda: rilevatori.Oggetti(a.categorie), "cerca", libera=True)


def rileva_volti(a: Analisi) -> None:
    if "sguardi" in a.categorie:
        rileva(a, "volti", a.volti, rilevatori.Volti, "frontali")


# --------------------------------------------------------------------------
# 5. Esame dei fotogrammi con Qwen3-VL
# --------------------------------------------------------------------------

def prepara_modelli(a: Analisi) -> None:
    rilevatori.avvia_ollama()
    for modello in [a.modello, a.modello_verifica] + ([a.seconda_opinione] if a.seconda_opinione else []):
        if not rilevatori.modello_installato(modello):
            sys.exit(f"Ollama non è acceso o manca il modello {modello}.")
    turno()
    if not rilevatori.modello_caricato(a.modello):
        # Il primo caricamento di Qwen3-VL in memoria richiede da mezzo minuto a un minuto e mezzo.
        fase("modello")
        rilevatori.carica_modello(a.modello)
        avanzamento("modello", 1, 1)


def passa_a(modello: str, da: str) -> None:
    """Un modello alla volta in memoria: quello che ha finito lascia il posto al prossimo (pochi secondi)."""
    if modello != da:
        coordinamento.libera_modello(da, rilevatori.CONTESTO)
        rilevatori.carica_modello(modello)


def esamina_fotogrammi(a: Analisi, fotogrammi: list) -> None:
    fase("esame")
    # Solo i fotogrammi non ancora esaminati: così il tempo stimato misura il lavoro vero.
    for n, p in enumerate(fotogrammi, 1):
        if p.name in a.illeggibili:
            a.non_esaminati.append(p.name)
        else:
            try:
                turno()
                a.esame.metti(f"{p.name}|{a.chiave_cat}", rilevatori.esamina(a.modello, p, a.categorie))
            except (RuntimeError, OSError) as e:
                print(f"{p.name}: {e}")
                a.non_esaminati.append(p.name)
        avanzamento("esame", n, len(fotogrammi))


# --------------------------------------------------------------------------
# 6. Verifica dei sospetti
# --------------------------------------------------------------------------

def _jpeg(cv2, img, qualita: int = 88) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualita])[1].tobytes()


def immagini_sospetto(percorso: Path, riquadro, categoria: str) -> tuple:
    """(fotogramma con il riquadro rosso, ritaglio ingrandito) come JPEG in memoria.

    OSError se il fotogramma non si legge."""
    import cv2
    img = cv2.imread(str(percorso))
    if img is None:
        raise OSError(f"fotogramma illeggibile: {percorso.name}")
    h, w = img.shape[:2]
    x0, y0, x1, y1 = riquadro or (0, 0, 1, 1)
    X0, Y0, X1, Y1 = int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)
    segnato = img.copy()
    spessore = max(2, w // 400)
    cv2.rectangle(segnato, (X0, Y0), (X1, Y1), (40, 40, 230), spessore)
    # Ritaglio: il riquadro allargato per dare contesto; per gli sguardi solo il volto.
    allarga = 1.5 if categoria == "sguardi" else 2.0
    minimo = 0.08 if categoria == "sguardi" else 0.22
    cw = max((X1 - X0) * allarga, w * minimo)
    ch = max((Y1 - Y0) * allarga, h * minimo)
    if categoria == "sguardi":
        cw = ch = max(cw, ch)
    cx, cy = (X0 + X1) / 2, (Y0 + Y1) / 2
    a0, b0 = int(max(0, cx - cw / 2)), int(max(0, cy - ch / 2))
    a1, b1 = int(min(w, cx + cw / 2)), int(min(h, cy + ch / 2))
    ritaglio = (img if categoria == "sguardi" else segnato)[b0:b1, a0:a1]
    lato = 512 if categoria == "sguardi" else 768
    scala = lato / max(ritaglio.shape[:2])
    ritaglio = cv2.resize(ritaglio, None, fx=scala, fy=scala,
                          interpolation=cv2.INTER_CUBIC if scala > 1 else cv2.INTER_AREA)
    return _jpeg(cv2, segnato), _jpeg(cv2, ritaglio)


def _sovrapposti(r1, r2) -> bool:
    if not r1 or not r2:
        return False
    ix = max(0, min(r1[2], r2[2]) - max(r1[0], r2[0]))
    iy = max(0, min(r1[3], r2[3]) - max(r1[1], r2[1]))
    inter = ix * iy
    unione = (r1[2] - r1[0]) * (r1[3] - r1[1]) + (r2[2] - r2[0]) * (r2[3] - r2[1]) - inter
    return unione > 0 and inter / unione > 0.4


def stima_verifiche(a: Analisi, esami: int) -> int:
    """Chiamate di verifica previste prima dell'esame: una per sospetto di OWLv2 (al più MAX_VERIFICHE per
    inquadratura e categoria) più quelli che troverà l'esame, circa uno ogni dieci fotogrammi esaminati."""
    da_owl = 0
    for q in a.inquadrature:
        per_cat = {}
        for _, p in a.fotogrammi[q.numero]:
            for r in a.oggetti.get(p.name, []):
                if r["categoria"] in a.categorie:
                    per_cat[r["categoria"]] = per_cat.get(r["categoria"], 0) + 1
        da_owl += sum(min(MAX_VERIFICHE, n) for n in per_cat.values())
    return da_owl + round(.1 * esami)


def sospetti_di(a: Analisi, q, cat: str) -> list:
    """(priorità, fotogramma, riquadro, origine, oggetto) dei sospetti da verificare, i più probabili prima."""
    foto = a.fotogrammi[q.numero]
    sospetti = [(0, p, r["riquadro"], "esame", r["voce"]) for _, p in foto
                for r in a.esame.get(f"{p.name}|{a.chiave_cat}", []) if r["categoria"] == cat]
    if cat in CATEGORIE_OGGETTI:
        trovati = [(r["punteggio"], p, r) for _, p in foto for r in a.oggetti.get(p.name, []) if r["categoria"] == cat]
        for punteggio, p, r in sorted(trovati, key=lambda x: -x[0]):
            sospetti.append((1 - punteggio, p, r["riquadro"], "oggetti", r["oggetto"]))
    return sorted(sospetti, key=lambda s: s[0])


class Segnalazioni:
    """Le segnalazioni dell'analisi e le loro immagini, in segnalazioni/."""

    def __init__(self, a: Analisi):
        self.a = a
        self.elenco = []
        self.cartella = a.out / "segnalazioni"
        self.cartella.mkdir(exist_ok=True)
        self.tempo_di = {p.name: t for q in a.inquadrature for t, p in a.fotogrammi[q.numero]}
        # Le immagini delle analisi precedenti non devono restare accanto alle nuove.
        for vecchia in self.cartella.glob("*.jpg"):
            vecchia.unlink()

    def aggiungi(self, q, cat, p, riquadro, segnato, dettaglio, campi) -> None:
        a, ident = self.a, f"{q.numero:05d}-{cat}"
        (self.cartella / f"{ident}.jpg").write_bytes(segnato)
        (self.cartella / f"{ident}_dettaglio.jpg").write_bytes(dettaglio)
        t = self.tempo_di.get(p.name, (q.inizio + q.fine) / 2)
        self.elenco.append({
            "id": ident, "numero": q.numero, "categoria": cat, "titolo": CATEGORIE[cat],
            "fotogrammi_totali": len(a.fotogrammi[q.numero]), "riquadro": riquadro,
            "tc": montaggio.secondi_a_tc(t + a.tc_inizio_s, a.fps), "secondi": round(t, 3),
            "tc_in": q.tc_in, "tc_out": q.tc_out, "clip": q.clip, "scena": q.scena(),
            "immagine": f"segnalazioni/{ident}.jpg", "dettaglio": f"segnalazioni/{ident}_dettaglio.jpg", **campi})


def segnala_sguardo(a: Analisi, s: Segnalazioni, q) -> None:
    """Gli sguardi sono misurati dal modello dello sguardo: nessuna verifica di Qwen3-VL."""
    trovati = sorted(((v["angolo"], p, v) for _, p in a.fotogrammi[q.numero] for v in a.volti.get(p.name, [])
                      if v["angolo"] <= rilevatori.soglia_sguardo(v["riquadro"])), key=lambda x: x[0])
    for angolo, p, v in trovati:
        try:
            segnato, dettaglio = immagini_sospetto(p, v["riquadro"], "sguardi")
        except OSError:
            a.illeggibili.add(p.name)
            continue
        di_comparsa = rilevatori.comparsa(v["riquadro"])
        s.aggiungi(q, "sguardi", p, v["riquadro"], segnato, dettaglio, {
            "descrizione": f"{'Comparsa che guarda' if di_comparsa else 'Sguardo'} a {angolo:.0f}° dall'obiettivo "
                           f"({v['orizzontale']:+.0f}° in orizzontale, {v['verticale']:+.0f}° in verticale).",
            "certezza": ("alta" if angolo <= rilevatori.SGUARDO_CERTO else "media") if di_comparsa else "bassa",
            **({} if di_comparsa else {"da_controllare": True, "seconda_opinione":
                "volto in primo piano: potrebbe guardare un personaggio appena accanto alla macchina da presa"}),
            "oggetto": "volto", "origine": "sguardo", "fotogrammi_sospetti": len({x[1].name for x in trovati})})
        return


def verifica_sospetti(a: Analisi) -> list:
    """Verifica a due livelli. Ogni sospetto passa dal modello di verifica (Gemma 4), su un ritaglio
    ingrandito; ciò che accetta (al più un sospetto per inquadratura e categoria) passa poi dalla seconda
    opinione (Qwen3-VL). Se sono d'accordo è una segnalazione normale; se no resta, ma «da controllare».
    Così non sparisce nulla in silenzio, e le revisioni dell'utente diranno quale dei due ha ragione."""
    fase("verifica")
    s = Segnalazioni(a)
    da_verificare = {(q.numero, cat): sospetti_di(a, q, cat)
                     for q in a.inquadrature for cat in a.categorie if cat != "sguardi"}
    if any(da_verificare.values()):
        passa_a(a.modello_verifica, a.modello)
    # Quante chiamate servono al massimo (quelle non ancora fatte): la barra avanza a ogni chiamata. Il
    # conteggio è per eccesso, perché ci si ferma al primo sospetto accettato; le seconde opinioni si
    # aggiungono quando se ne conosce il numero.
    chiamate = sum(1 for (_, cat), lista in da_verificare.items() for _, p, riquadro, _, _ in lista[:MAX_VERIFICHE]
                   if f"{p.name}|{cat}|{riquadro}" not in a.verifiche)
    piano(verifica=chiamate)
    conta = {"fatte": 0, "totale": chiamate}

    def chiama(archivio: Archivio, modello: str, k: str, cat: str, immagini: list, q) -> dict:
        if k not in archivio:
            try:
                turno()
                archivio.metti(k, rilevatori.verifica(modello, cat, immagini))
            except RuntimeError as e:
                print(f"Inquadratura {q.numero}: {e}")
                return None
            finally:
                conta["fatte"] += 1
                avanzamento("verifica", min(conta["fatte"], conta["totale"]), max(conta["totale"], 1))
        return archivio[k]

    accettati = []
    for q in a.inquadrature:
        for cat in (c for c in CATEGORIE if c in a.categorie):
            if cat == "sguardi":
                segnala_sguardo(a, s, q)
                continue
            sospetti = da_verificare[(q.numero, cat)]
            visti = len({x[1].name for x in sospetti})
            scartati, tentati = [], 0
            for _, p, riquadro, origine, oggetto in sospetti:
                if tentati >= MAX_VERIFICHE:
                    break
                if any(_sovrapposti(riquadro, x) for x in scartati):
                    continue
                tentati += 1
                k = f"{p.name}|{cat}|{riquadro}"
                try:
                    segnato, dettaglio = immagini_sospetto(p, riquadro, cat)
                except OSError as e:
                    print(f"Inquadratura {q.numero}: {e}")
                    a.illeggibili.add(p.name)
                    continue
                v = chiama(a.verifiche, a.modello_verifica, k, cat, [segnato, dettaglio], q)
                if v is None:
                    a.non_verificate.add(q.numero)
                    continue
                if not v.get("errore"):
                    scartati.append(riquadro)
                    continue
                accettati.append((q, cat, p, riquadro, segnato, dettaglio, k, {
                    "descrizione": v.get("cosa_vedo", ""), "certezza": v.get("certezza", "media"),
                    "oggetto": v.get("classe") or oggetto, "origine": origine, "fotogrammi_sospetti": visti}))
                break

    if a.seconda_opinione and a.seconda_opinione != a.modello_verifica:
        conta["totale"] = conta["fatte"] + sum(1 for x in accettati if x[6] not in a.seconde)
        piano(verifica=conta["totale"])
        passa_a(a.seconda_opinione, a.modello_verifica)
    for q, cat, p, riquadro, segnato, dettaglio, k, campi in accettati:
        if a.seconda_opinione and a.seconda_opinione != a.modello_verifica:
            seconda = chiama(a.seconde, a.seconda_opinione, k, cat, [segnato, dettaglio], q)
            if not (seconda and seconda.get("errore")):
                campi = {**campi, "certezza": "bassa", "da_controllare": True,
                         "seconda_opinione": (seconda or {}).get("cosa_vedo") or "non disponibile"}
        s.aggiungi(q, cat, p, riquadro, segnato, dettaglio, campi)
    ordine = list(CATEGORIE)
    return sorted(s.elenco, key=lambda x: (x["numero"], ordine.index(x["categoria"])))


# --------------------------------------------------------------------------
# Fine
# --------------------------------------------------------------------------

def concludi(a: Analisi, segnalazioni: list) -> None:
    archivi.scrivi_json(a.out / "segnalazioni.json", {
        "video": str(a.video), "montaggio": str(a.args.montaggio or ""), "fps": a.fps, "tc_inizio": a.tc_inizio,
        "inquadrature": len(a.inquadrature), "categorie": sorted(a.categorie),
        "creato": time.strftime("%Y-%m-%d %H:%M"), "segnalazioni": segnalazioni})
    esporta.tutto(a.out)
    if a.illeggibili:
        avviso(f"{quanti(len(a.illeggibili), 'fotogramma non si è potuto leggere ed è stato saltato', 'fotogrammi non si sono potuti leggere e sono stati saltati')}"
               f": forse il disco ha dato un errore. Cancellando i fotogrammi con «Libera spazio…» e rilanciando "
               f"vengono ricreati.")
    dubbie = sum(1 for x in segnalazioni if x.get("da_controllare"))
    if dubbie:
        avviso(f"{quanti(dubbie, 'segnalazione è da controllare', 'segnalazioni sono da controllare')}: "
               f"la verifica le ritiene errori, la seconda opinione no. Le trovi con il filtro «Da controllare».")
    esaminabili = [x for x in a.non_esaminati if x not in a.illeggibili]
    if esaminabili:
        avviso(f"{quanti(len(esaminabili), 'fotogramma non è stato esaminato', 'fotogrammi non sono stati esaminati')}"
               f": il modello ({a.modello}) non ha risposto. Rilanciando l'analisi vengono ripresi solo quelli.")
    if a.non_verificate:
        avviso(f"In {quanti(len(a.non_verificate), 'inquadratura', 'inquadrature')} alcuni sospetti non sono stati "
               f"verificati: il modello ({a.modello_verifica}) non ha risposto (inquadrature "
               f"{elenco(a.non_verificate)}). Rilanciando l'analisi vengono ripresi.")
    senza_foto = [q.numero for q in a.inquadrature if not a.fotogrammi[q.numero]]
    if senza_foto:
        avviso(f"{quanti(len(senza_foto), 'inquadratura non controllata', 'inquadrature non controllate')} perché "
               f"non se ne sono potuti estrarre i fotogrammi: {elenco(senza_foto)}.")
    a.finita = True
    fase("fine")
    conteggio = {CATEGORIE[c]: sum(1 for x in segnalazioni if x["categoria"] == c) for c in CATEGORIE if c in a.categorie}
    print(f"Fatto: {len(segnalazioni)} segnalazioni su {len(a.inquadrature)} inquadrature. "
          + "; ".join(f"{k}: {v}" for k, v in conteggio.items()))
    print(f"Report: {a.out / 'report_murnau.html'}")


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------

def leggi_argomenti(argv=None):
    ap = argparse.ArgumentParser(description="Cerca gli errori di ripresa in un film montato, solo in locale.")
    ap.add_argument("video", type=Path)
    ap.add_argument("--montaggio", type=Path, help="EDL CMX3600 o AAF della sequenza (consigliato)")
    ap.add_argument("--out", type=Path, default=Path("risultati_murnau"))
    ap.add_argument("--categorie", default=",".join(CATEGORIE),
                    help="categorie da cercare, separate da virgole: " + ", ".join(CATEGORIE))
    ap.add_argument("--passo", type=float, default=1.0,
                    help="secondi tra un fotogramma e l'altro per oggetti e volti (predefinito 1)")
    ap.add_argument("--accurato", action="store_true",
                    help="Qwen3-VL esamina un fotogramma ogni 2 secondi invece di 1-4 per inquadratura (più lento)")
    ap.add_argument("--modello", default=rilevatori.MODELLO_ESAME, help="modello che esamina i fotogrammi")
    ap.add_argument("--modello-verifica", default=rilevatori.MODELLO_VERIFICA, help="modello che verifica i sospetti")
    ap.add_argument("--seconda-opinione", default=rilevatori.MODELLO_ESAME,
                    help="modello che ricontrolla ciò che la verifica accetta (\"\" per non ricontrollare)")
    ap.add_argument("--tc-inizio", help="timecode del primo fotogramma del video, se il file non lo dichiara")
    ap.add_argument("--da", type=int, default=1, help="prima inquadratura da analizzare")
    ap.add_argument("--a", type=int, help="ultima inquadratura da analizzare")
    ap.add_argument("--soglia-tagli", type=float, default=0.3, help="sensibilità del rilevamento tagli senza montaggio")
    return ap.parse_args(argv)


def main() -> None:
    a = Analisi(leggi_argomenti())
    sconosciute = a.categorie - set(CATEGORIE)
    if sconosciute:
        sys.exit(f"Categorie sconosciute: {', '.join(sconosciute)}")
    if not a.video.exists():
        sys.exit(f"Video non trovato: {a.video}")
    a.lavoro.mkdir(parents=True, exist_ok=True)

    # Ogni analisi si registra, così l'app la vede anche se è partita da Terminale; all'uscita
    # (anche se interrotta con Ferma, ⌃C o dalla chiusura dell'app) si toglie e libera il modello.
    progresso.registro = coordinamento.Registro(str(a.video), str(a.out))
    atexit.register(chiusura, a)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    coordinamento.sorveglia_app()
    tieni_sveglio()
    if progresso.registro.dati["origine"] == "terminale":
        coordinamento.notifica(f"Analisi avviata da Terminale: {a.video.name}. La vedi e la puoi fermare dall'app.")

    prepara_inquadrature(a)
    estrai_fotogrammi(a)
    a.archivi()
    # Lavoro esatto che resta, al netto di quanto è già stato fatto in un'esecuzione precedente.
    esami = da_esaminare(a)
    tutti = a.tutti_i_fotogrammi()
    piano(**unita_dei_rilevatori(a, sum(p.name not in a.oggetti for p in tutti), sum(p.name not in a.volti for p in tutti)),
          modello=0 if rilevatori.modello_caricato(a.modello) else 1, esame=len(esami), verifica=2 * len(a.inquadrature))
    rileva_oggetti(a)
    rileva_volti(a)
    # Ora i sospetti di OWLv2 sono noti: la stima delle verifiche diventa molto più precisa.
    piano(verifica=stima_verifiche(a, len(esami)))
    if a.categorie - {"sguardi"}:  # gli sguardi da soli non usano i modelli di Ollama
        prepara_modelli(a)
    esamina_fotogrammi(a, esami)
    concludi(a, verifica_sospetti(a))


if __name__ == "__main__":
    main()
