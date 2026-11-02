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
(tramite Ollama) esamina i fotogrammi e verifica ogni sospetto su un ritaglio
ingrandito.

    python murnau.py FILM.mov --montaggio FILM.aaf --out risultati
"""

import argparse
import atexit
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import coordinamento
import esporta
import montaggio
import rilevatori
from rilevatori import CATEGORIE

LARGHEZZA = 1280           # pixel dei fotogrammi analizzati
MAX_VERIFICHE = 3          # sospetti verificati al massimo per inquadratura e categoria


REGISTRO = None      # coordinamento.Registro di questa analisi
FFMPEG = None        # l'estrazione dei fotogrammi in corso, da fermare se l'analisi si interrompe
FINITA = False


def fase(nome: str) -> None:
    print(f"@@fase {nome}", flush=True)
    if REGISTRO:
        REGISTRO.aggiorna(fase=nome, fatti=0, totale=0)


def avanzamento(nome: str, fatti: int, totale: int) -> None:
    if totale <= 200 or fatti % max(1, totale // 200) == 0 or fatti == totale:
        print(f"@@avanz {nome} {fatti} {totale}", flush=True)
    if REGISTRO:
        REGISTRO.aggiorna(fatti=fatti, totale=totale)


def avviso(testo: str) -> None:
    """Una cosa da verificare: l'app la elenca nel riquadro che compare a fine lavoro."""
    print(f"@@avviso {testo}", flush=True)


def attesa(testo: str) -> None:
    """Avvisa l'app che l'analisi è ferma in attesa (stringa vuota: si riparte)."""
    print(f"@@attesa {testo}".rstrip(), flush=True)
    if REGISTRO:
        REGISTRO.aggiorna(attesa=testo)


_ULTIMO_TURNO = [0.0]


def turno() -> None:
    """Prima di usare il modello: se Vertov sta analizzando, gli si lascia il modello.
    Si controlla al più ogni 30 secondi: costa poco ma non serve farlo a ogni fotogramma."""
    if time.time() - _ULTIMO_TURNO[0] > 30:
        coordinamento.aspetta_il_turno(attesa)
        _ULTIMO_TURNO[0] = time.time()


def chiusura(modello: str, video: Path) -> None:
    """All'uscita, normale o interrotta: niente resta acceso e il modello non resta in memoria."""
    if FFMPEG and FFMPEG.poll() is None:
        FFMPEG.kill()
    if REGISTRO:
        REGISTRO.chiudi()
        if REGISTRO.dati["origine"] == "terminale":
            coordinamento.notifica(f"{'Analisi finita' if FINITA else 'Analisi interrotta'}: {video.name}")
    coordinamento.libera_modello(modello, rilevatori.CONTESTO)


# Secondi stimati per unità di lavoro di ogni fase (M5 Max, Ollama libero): servono a pesare le
# fasi nella percentuale complessiva la prima volta; poi l'app usa i tempi misurati nelle analisi
# precedenti su questo Mac.
COSTI = {"fotogrammi": .04, "oggetti": .6, "volti": .08, "modello": 45.0, "esame": 4.5, "verifica": 3.5}


def piano(**unita) -> None:
    """Annuncia all'app quanto lavoro resta in ogni fase, nell'ordine in cui verrà fatto."""
    print("@@piano " + " ".join(f"{nome}={n}:{COSTI[nome]}" for nome, n in unita.items()), flush=True)


class Archivio:
    """Risultati già calcolati, in un file JSONL: permette di riprendere un'analisi interrotta."""

    def __init__(self, percorso: Path):
        self.percorso = percorso
        self.dati = {}
        if percorso.exists():
            for riga in percorso.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(riga)
                    self.dati[r["k"]] = r["v"]
                except (ValueError, KeyError):
                    pass  # riga troncata da un'interruzione

    def __contains__(self, k):
        return k in self.dati

    def __getitem__(self, k):
        return self.dati[k]

    def metti(self, k: str, v) -> None:
        self.dati[k] = v
        with self.percorso.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Fotogrammi
# --------------------------------------------------------------------------

def estrai_fotogrammi(video: Path, inquadrature: list, cartella: Path, passo: float,
                      ritaglio: str, fps: float) -> dict:
    """Un fotogramma ogni `passo` secondi, in un solo passaggio sul film.

    Restituisce {numero inquadratura: [(secondi, percorso)]}. I fotogrammi a ridosso
    dei tagli vengono scartati; le inquadrature più brevi del passo ricevono il loro
    fotogramma centrale.
    """
    inizio = max(0.0, math.floor(min(q.inizio for q in inquadrature) / passo) * passo)
    fine = max(q.fine for q in inquadrature)
    filtri = (f"{ritaglio}," if ritaglio else "") + f"fps=1/{passo},scale={LARGHEZZA}:-2"
    completo = cartella / ".completo"
    firma = json.dumps({"inizio": inizio, "fine": fine, "passo": passo, "filtri": filtri})
    if not completo.exists() or completo.read_text() != firma:
        shutil.rmtree(cartella, ignore_errors=True)
        cartella.mkdir(parents=True)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-ss", f"{inizio:.3f}", "-to", f"{fine:.3f}", "-i", str(video), "-an", "-sn",
               "-vf", filtri, "-q:v", "3", "-start_number", "0", str(cartella / "f%06d.jpg")]
        global FFMPEG
        proc = FFMPEG = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        totale = round(fine - inizio)
        for riga in proc.stdout:
            m = re.match(r"out_time_us=(\d+)", riga)
            if m:
                avanzamento("fotogrammi", min(totale, int(m.group(1)) // 1_000_000), totale)
        if proc.wait() != 0:
            sys.exit("ffmpeg non è riuscito a leggere il video.")
        completo.write_text(firma)

    margine = 1.5 / fps
    per_inquadratura = {q.numero: [] for q in inquadrature}
    tempi = sorted((inizio + int(p.stem[1:]) * passo, p) for p in cartella.glob("f*.jpg"))
    i = 0
    for q in inquadrature:
        while i < len(tempi) and tempi[i][0] < q.inizio + margine:
            i += 1
        j = i
        while j < len(tempi) and tempi[j][0] <= q.fine - margine:
            per_inquadratura[q.numero].append(tempi[j])
            j += 1
        if not per_inquadratura[q.numero]:
            t = (q.inizio + q.fine) / 2
            dest = cartella / f"q{q.numero:05d}.jpg"
            if not dest.exists():
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                                "-frames:v", "1", "-vf", (f"{ritaglio}," if ritaglio else "") + f"scale={LARGHEZZA}:-2",
                                "-q:v", "3", "-y", str(dest)])
            if dest.exists():
                per_inquadratura[q.numero].append((t, dest))
    return per_inquadratura


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


# --------------------------------------------------------------------------
# Immagini per la verifica e per il report
# --------------------------------------------------------------------------

def _jpeg(cv2, img, qualita: int = 88) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualita])[1].tobytes()


def immagini_sospetto(percorso: Path, riquadro, categoria: str) -> tuple:
    """(fotogramma con il riquadro rosso, ritaglio ingrandito) come JPEG in memoria."""
    import cv2
    img = cv2.imread(str(percorso))
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


def _sovrapposti(a, b) -> bool:
    if not a or not b:
        return False
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    unione = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return unione > 0 and inter / unione > 0.4


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------

def main() -> None:
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
    ap.add_argument("--modello", default=rilevatori.MODELLO_PREDEFINITO)
    ap.add_argument("--tc-inizio", help="timecode del primo fotogramma del video, se il file non lo dichiara")
    ap.add_argument("--da", type=int, default=1, help="prima inquadratura da analizzare")
    ap.add_argument("--a", type=int, help="ultima inquadratura da analizzare")
    ap.add_argument("--soglia-tagli", type=float, default=0.3, help="sensibilità del rilevamento tagli senza montaggio")
    args = ap.parse_args()

    categorie = {c.strip() for c in args.categorie.split(",") if c.strip()}
    sconosciute = categorie - set(CATEGORIE)
    if sconosciute:
        sys.exit(f"Categorie sconosciute: {', '.join(sconosciute)}")
    if not args.video.exists():
        sys.exit(f"Video non trovato: {args.video}")
    out = args.out
    lavoro = out / "lavoro"
    lavoro.mkdir(parents=True, exist_ok=True)
    (out / "segnalazioni").mkdir(exist_ok=True)

    # Ogni analisi si registra, così l'app la vede anche se è partita da Terminale; all'uscita
    # (anche se interrotta con Ferma, ⌃C o dalla chiusura dell'app) si toglie e libera il modello.
    global REGISTRO, FINITA
    REGISTRO = coordinamento.Registro(str(args.video), str(out))
    atexit.register(chiusura, args.modello, args.video)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    coordinamento.sorveglia_app()
    if REGISTRO.dati["origine"] == "terminale":
        coordinamento.notifica(f"Analisi avviata da Terminale: {args.video.name}. La vedi e la puoi fermare dall'app.")

    # 1. Inquadrature
    fase("preparazione")
    fps, durata, tc_file = montaggio.info_video(args.video)
    tc_inizio = args.tc_inizio or tc_file or "00:00:00:00"
    tc_inizio_s = montaggio.tc_a_secondi(tc_inizio, fps)
    print(f"Video: {durata / 60:.1f} minuti a {fps:g} fps, inizia a {tc_inizio}")
    if args.montaggio:
        inquadrature = montaggio.leggi_montaggio(args.montaggio, fps, tc_inizio_s)
        fuori = [q for q in inquadrature if q.inizio < -0.5 or q.fine > durata + 0.5]
        if len(fuori) > len(inquadrature) / 2:
            sys.exit(f"Il montaggio non corrisponde al video: {len(fuori)} inquadrature su {len(inquadrature)} cadono "
                     f"fuori dal file. Controlla il timecode di inizio (ora {tc_inizio}).")
        saltate = [q for q in fuori if q.fine <= 0 or q.inizio >= durata]
        tagliate = [q for q in fuori if q not in saltate]
        for gruppo, cosa in ((saltate, "fuori dal film e non è stata controllata|fuori dal film e non sono state controllate"),
                             (tagliate, "in parte fuori dal film ed è stata controllata solo per il tratto che c'è|"
                                        "in parte fuori dal film e sono state controllate solo per il tratto che c'è")):
            if gruppo:
                uno, molti = cosa.split("|")
                avviso((f"1 inquadratura del montaggio cade {uno}" if len(gruppo) == 1 else
                        f"{len(gruppo)} inquadrature del montaggio cadono {molti}") +
                       f". Verifica che film e montaggio siano della stessa versione (inizio del film: {tc_inizio}).")
        inquadrature = [q for q in inquadrature if q.fine > 0 and q.inizio < durata]
        for q in inquadrature:
            q.inizio, q.fine = max(0.0, q.inizio), min(durata, q.fine)
    else:
        print("Nessun EDL o AAF: rilevo i tagli con ffmpeg...")
        inquadrature = montaggio.rileva_tagli(args.video, durata, fps, tc_inizio_s, args.soglia_tagli)
    inquadrature = [q for q in inquadrature if q.numero >= args.da and (args.a is None or q.numero <= args.a)]
    if not inquadrature:
        sys.exit("Nessuna inquadratura da analizzare.")
    print(f"{len(inquadrature)} inquadrature da analizzare")
    ritaglio = montaggio.rileva_bande_nere(args.video, durata)
    if ritaglio:
        print(f"Bande nere: analizzo solo l'immagine ({ritaglio})")
    con_oggetti = bool(categorie & {"troupe", "lavalier", "prese_cavi"})
    # Prima stima del lavoro, dalle durate: diventa esatta dopo l'estrazione dei fotogrammi.
    secondi = round(sum(q.fine - q.inizio for q in inquadrature))
    stimati = [max(1, round((q.fine - q.inizio) / args.passo)) for q in inquadrature]
    esami = sum(len(scegli_per_esame([None] * n, q.fine - q.inizio, args.accurato)) for q, n in zip(inquadrature, stimati))
    piano(fotogrammi=secondi, **({"oggetti": sum(stimati)} if con_oggetti else {}),
          **({"volti": sum(stimati)} if "sguardi" in categorie else {}),
          modello=0 if rilevatori.modello_caricato(args.modello) else 1, esame=esami, verifica=2 * len(inquadrature))

    # 2. Fotogrammi
    fase("fotogrammi")
    fotogrammi = estrai_fotogrammi(args.video, inquadrature, lavoro / "fotogrammi", args.passo, ritaglio, fps)
    tutti = [p for q in inquadrature for _, p in fotogrammi[q.numero]]
    print(f"{len(tutti)} fotogrammi estratti")

    # Lavoro esatto che resta, al netto di quanto è già stato fatto in un'esecuzione precedente.
    oggetti = Archivio(lavoro / "oggetti.jsonl")
    volti = Archivio(lavoro / "sguardi.jsonl")
    esame = Archivio(lavoro / "esame.jsonl")
    chiave_cat = ",".join(sorted(categorie))
    da_esaminare = [p for q in inquadrature
                    for _, p in scegli_per_esame(fotogrammi[q.numero], q.fine - q.inizio, args.accurato)
                    if f"{p.name}|{chiave_cat}" not in esame]
    oggetti_da_fare = [p for p in tutti if p.name not in oggetti] if con_oggetti else []
    volti_da_fare = [p for p in tutti if p.name not in volti] if "sguardi" in categorie else []
    piano(**({"oggetti": len(oggetti_da_fare)} if con_oggetti else {}),
          **({"volti": len(volti_da_fare)} if "sguardi" in categorie else {}),
          modello=0 if rilevatori.modello_caricato(args.modello) else 1,
          esame=len(da_esaminare), verifica=2 * len(inquadrature))

    # 3. Oggetti (OWLv2)
    if con_oggetti:
        fase("oggetti")
        da_fare = oggetti_da_fare
        if da_fare:
            print("Carico il rilevatore di oggetti (OWLv2)...")
            rilevatore = rilevatori.Oggetti(categorie)
            for n, p in enumerate(da_fare, 1):
                oggetti.metti(p.name, rilevatore.cerca(p))
                avanzamento("oggetti", n, len(da_fare))
            del rilevatore

    # 4. Volti frontali
    if "sguardi" in categorie:
        fase("volti")
        da_fare = volti_da_fare
        if da_fare:
            rilevatore = rilevatori.Volti()
            for n, p in enumerate(da_fare, 1):
                volti.metti(p.name, rilevatore.frontali(p))
                avanzamento("volti", n, len(da_fare))

    # Ora i sospetti di OWLv2 sono noti: la stima delle verifiche diventa molto più precisa
    # (una chiamata per sospetto, al massimo MAX_VERIFICHE per inquadratura e categoria, più
    # quelli che troverà l'esame, circa uno ogni dieci fotogrammi esaminati).
    da_owl = 0
    for q in inquadrature:
        per_cat = {}
        for _, p in fotogrammi[q.numero]:
            for r in (oggetti[p.name] if p.name in oggetti else []):
                if r["categoria"] in categorie:
                    per_cat[r["categoria"]] = per_cat.get(r["categoria"], 0) + 1
        da_owl += sum(min(MAX_VERIFICHE, n) for n in per_cat.values())
    piano(verifica=da_owl + round(.1 * len(da_esaminare)))

    # 5. Esame dei fotogrammi con Qwen3-VL
    rilevatori.avvia_ollama()
    if not rilevatori.ollama_pronto(args.modello):
        sys.exit(f"Ollama non è acceso o manca il modello {args.modello}.")
    turno()
    if not rilevatori.modello_caricato(args.modello):
        # Il primo caricamento di Qwen3-VL in memoria richiede da mezzo minuto a un minuto e mezzo.
        fase("modello")
        rilevatori.carica_modello(args.modello)
        avanzamento("modello", 1, 1)
    fase("esame")
    non_esaminati = []
    # Solo i fotogrammi non ancora esaminati: così il tempo stimato misura il lavoro vero.
    for n, p in enumerate(da_esaminare, 1):
        try:
            turno()
            esame.metti(f"{p.name}|{chiave_cat}", rilevatori.esamina(args.modello, p, categorie))
        except RuntimeError as e:
            print(f"{p.name}: {e}")
            non_esaminati.append(p.name)
        avanzamento("esame", n, len(da_esaminare))

    # 6. Verifica dei sospetti
    fase("verifica")
    non_verificate = set()
    verifiche = Archivio(lavoro / "verifiche.jsonl")
    tempo_di = {p.name: t for q in inquadrature for t, p in fotogrammi[q.numero]}
    segnalazioni = []
    # Le immagini delle analisi precedenti non devono restare accanto alle nuove.
    for vecchia in (out / "segnalazioni").glob("*.jpg"):
        vecchia.unlink()

    def segnalazione(q, cat, p, riquadro, segnato, dettaglio, campi) -> dict:
        ident = f"{q.numero:05d}-{cat}"
        (out / "segnalazioni" / f"{ident}.jpg").write_bytes(segnato)
        (out / "segnalazioni" / f"{ident}_dettaglio.jpg").write_bytes(dettaglio)
        t = tempo_di.get(p.name, (q.inizio + q.fine) / 2)
        return {"id": ident, "numero": q.numero, "categoria": cat, "titolo": CATEGORIE[cat],
                "fotogrammi_totali": len(fotogrammi[q.numero]), "riquadro": riquadro,
                "tc": montaggio.secondi_a_tc(t + tc_inizio_s, fps), "secondi": round(t, 3),
                "tc_in": q.tc_in, "tc_out": q.tc_out, "clip": q.clip, "scena": q.scena(),
                "immagine": f"segnalazioni/{ident}.jpg", "dettaglio": f"segnalazioni/{ident}_dettaglio.jpg", **campi}

    def sospetti_di(q, cat) -> list:
        """(priorità, fotogramma, riquadro, origine, oggetto) dei sospetti da verificare, i più probabili prima."""
        foto = fotogrammi[q.numero]
        sospetti = []
        for _, p in foto:
            for r in (esame[f"{p.name}|{chiave_cat}"] if f"{p.name}|{chiave_cat}" in esame else []):
                if r["categoria"] == cat:
                    sospetti.append((0, p, r["riquadro"], "esame", r["voce"]))
        if cat in ("troupe", "lavalier", "prese_cavi"):
            trovati = [(r["punteggio"], p, r) for _, p in foto for r in (oggetti[p.name] if p.name in oggetti else [])
                       if r["categoria"] == cat]
            for punteggio, p, r in sorted(trovati, key=lambda x: -x[0]):
                sospetti.append((1 - punteggio, p, r["riquadro"], "oggetti", r["oggetto"]))
        return sorted(sospetti, key=lambda s: s[0])

    # Quante chiamate a Qwen3-VL servono al massimo (quelle non ancora fatte): la barra avanza a ogni
    # chiamata. Il conteggio è per eccesso, perché ci si ferma al primo sospetto confermato.
    da_verificare = {(q.numero, cat): sospetti_di(q, cat) for q in inquadrature for cat in categorie if cat != "sguardi"}
    chiamate = sum(1 for (_, cat), lista in da_verificare.items() for _, p, riquadro, _, _ in lista[:MAX_VERIFICHE]
                   if f"{p.name}|{cat}|{riquadro}" not in verifiche)
    piano(verifica=chiamate)
    fatte = 0

    for q in inquadrature:
        foto = fotogrammi[q.numero]
        for cat in (c for c in CATEGORIE if c in categorie):
            sospetti = da_verificare.get((q.numero, cat), [])
            if cat == "sguardi":
                # Misurati dal modello dello sguardo: nessuna verifica di Qwen3-VL.
                trovati = sorted(((v["angolo"], p, v) for _, p in foto for v in (volti[p.name] if p.name in volti else [])
                                  if v["angolo"] <= rilevatori.SOGLIA_SGUARDO), key=lambda x: x[0])
                if trovati:
                    angolo, p, v = trovati[0]
                    segnato, dettaglio = immagini_sospetto(p, v["riquadro"], cat)
                    segnalazioni.append(segnalazione(q, cat, p, v["riquadro"], segnato, dettaglio, {
                        "descrizione": f"Sguardo a {angolo:.0f}° dall'obiettivo "
                                       f"({v['orizzontale']:+.0f}° in orizzontale, {v['verticale']:+.0f}° in verticale).",
                        "certezza": "alta" if angolo <= rilevatori.SGUARDO_CERTO else "media",
                        "oggetto": "volto", "origine": "sguardo",
                        "fotogrammi_sospetti": len({x[1].name for x in trovati})}))
                continue
            if not sospetti:
                continue
            visti = len({s[1].name for s in sospetti})
            scartati, tentati = [], 0
            for _, p, riquadro, origine, oggetto in sospetti:
                if tentati >= MAX_VERIFICHE:
                    break
                if any(_sovrapposti(riquadro, s) for s in scartati):
                    continue
                if cat == "sguardi" and not riquadro:
                    continue
                tentati += 1
                k = f"{p.name}|{cat}|{riquadro}"
                segnato, dettaglio = immagini_sospetto(p, riquadro, cat)
                if k not in verifiche:
                    immagini = [dettaglio] if cat == "sguardi" else [segnato, dettaglio]
                    try:
                        turno()
                        verifiche.metti(k, rilevatori.verifica(args.modello, cat, immagini))
                    except RuntimeError as e:
                        print(f"Inquadratura {q.numero}: {e}")
                        non_verificate.add(q.numero)
                        continue
                    finally:
                        fatte += 1
                        avanzamento("verifica", min(fatte, chiamate), max(chiamate, 1))
                v = verifiche[k]
                if not v.get("errore"):
                    scartati.append(riquadro)
                    continue
                segnalazioni.append(segnalazione(q, cat, p, riquadro, segnato, dettaglio, {
                    "descrizione": v.get("cosa_vedo", ""), "certezza": v.get("certezza", "media"),
                    "oggetto": v.get("classe") or oggetto, "origine": origine, "fotogrammi_sospetti": visti}))
                break

    dati = {"video": str(args.video), "montaggio": str(args.montaggio or ""), "fps": fps,
            "tc_inizio": tc_inizio, "inquadrature": len(inquadrature), "categorie": sorted(categorie),
            "creato": time.strftime("%Y-%m-%d %H:%M"), "segnalazioni": segnalazioni}
    (out / "segnalazioni.json").write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")
    esporta.tutto(out)
    if non_esaminati:
        avviso(f"{len(non_esaminati)} fotogrammi non sono stati esaminati: Qwen3-VL non ha risposto. Rilanciando "
               f"l'analisi vengono ripresi solo quelli.")
    if non_verificate:
        avviso(f"In {len(non_verificate)} inquadrature alcuni sospetti non sono stati verificati: Qwen3-VL non ha "
               f"risposto (inquadrature {', '.join(map(str, sorted(non_verificate)[:20]))}"
               f"{'…' if len(non_verificate) > 20 else ''}). Rilanciando l'analisi vengono ripresi.")
    senza_foto = [q.numero for q in inquadrature if not fotogrammi[q.numero]]
    if senza_foto:
        avviso(f"{len(senza_foto)} inquadrature non controllate perché non se ne sono potuti estrarre i fotogrammi: "
               f"{', '.join(map(str, senza_foto[:20]))}{'…' if len(senza_foto) > 20 else ''}.")
    FINITA = True
    fase("fine")
    conteggio = {CATEGORIE[c]: sum(1 for s in segnalazioni if s["categoria"] == c) for c in CATEGORIE if c in categorie}
    print(f"Fatto: {len(segnalazioni)} segnalazioni su {len(inquadrature)} inquadrature. "
          + "; ".join(f"{k}: {v}" for k, v in conteggio.items()))
    print(f"Report: {out / 'report_murnau.html'}")


if __name__ == "__main__":
    main()
