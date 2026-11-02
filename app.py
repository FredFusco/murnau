#!/usr/bin/env python3
"""Murnau: l'app.

Apre una finestra propria (pywebview, WebKit di macOS) con l'interfaccia, servita
da un piccolo server interno raggiungibile solo da questo computer. Fa scegliere
il film e il montaggio, lancia l'analisi (murnau.py), ne mostra l'avanzamento
e permette di rivedere le segnalazioni una per una ed esportare i marker.
Ogni lavoro è una sessione salvata, che si può riaprire in seguito. I file con cui Murnau
lavora stanno in una cartella di lavoro per sessione; nella cartella scelta dall'utente
finisce solo ciò che esporta.

    ~/.venvs/descrittore/bin/python app.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aggiornamenti
import coordinamento
import esporta
import montaggio
import pulizia
import rilevatori

CARTELLA = Path(__file__).resolve().parent
PORTA = 8766
DATI = Path.home() / "Library" / "Application Support" / "Murnau"
SESSIONI = DATI / "sessioni"
FILE_CORRENTE = DATI / "sessione_corrente.json"
FILE_VECCHIO = DATI / "progetto.json"  # prima delle sessioni
REGISTRO = DATI / "murnau.log"
FILE_VELOCITA = DATI / "velocita.json"  # secondi per unità di ogni fase, misurati nelle analisi precedenti
MODELLO = rilevatori.MODELLO_PREDEFINITO
ESTENSIONI_VIDEO = {".mov", ".mp4", ".mxf", ".m4v", ".avi", ".mkv"}
ESTENSIONI_MONTAGGIO = {".edl", ".aaf"}
TIPI_IMMAGINE = {".jpg": "image/jpeg", ".png": "image/png"}
RUOLI = {"video": "Film", "montaggio": "Montaggio"}
# I file con cui Murnau lavora (fotogrammi, rilevamenti, segnalazioni, revisione...) stanno qui, dentro
# la cartella di Murnau, una cartella per sessione. Nella cartella di destinazione scelta dall'utente
# finisce solo ciò che esporta. Git, il pacchetto degli aggiornamenti e l'installazione degli
# aggiornamenti la ignorano (vedi .gitignore e aggiornamenti.INTOCCABILI).
NOME_FILE_DI_LAVORO = "Murnau – file di lavoro"
FILE_DI_LAVORO = CARTELLA / NOME_FILE_DI_LAVORO


# --------------------------------------------------------------------------
# Sessioni
# --------------------------------------------------------------------------
# Una sessione è un progetto di lavoro: il film, il montaggio, la sua cartella di
# lavoro, la cartella in cui esportare, le opzioni e un nome. Ogni modifica viene
# salvata subito.

def _file_sessione(id_: str) -> Path:
    return SESSIONI / f"{id_}.json"


def _scrivi_sessione(p: dict) -> None:
    SESSIONI.mkdir(parents=True, exist_ok=True)
    p["modificata"] = time.strftime("%Y-%m-%d %H:%M")
    _file_sessione(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1))
    FILE_CORRENTE.write_text(json.dumps({"id": p["id"]}))


def nuova_sessione(nome: str = "", dati: dict = None) -> dict:
    id_ = time.strftime("%Y%m%d-%H%M%S")
    while _file_sessione(id_).exists():
        id_ += "b"
    p = {"categorie": list(rilevatori.CATEGORIE), "accurato": False, **(dati or {}),
         "id": id_, "nome": nome.strip() or "Nuova sessione", "creata": time.strftime("%Y-%m-%d %H:%M")}
    _scrivi_sessione(p)
    return p


def carica_progetto() -> dict:
    try:
        id_ = json.loads(FILE_CORRENTE.read_text())["id"]
        return json.loads(_file_sessione(id_).read_text())
    except (OSError, ValueError, KeyError):
        pass
    # Primo avvio con le sessioni: il progetto di prima diventa la prima sessione.
    try:
        vecchio = json.loads(FILE_VECCHIO.read_text())
    except (OSError, ValueError):
        vecchio = {}
    p = nuova_sessione(Path(vecchio["video"]).stem if vecchio.get("video") else "", vecchio)
    FILE_VECCHIO.unlink(missing_ok=True)
    return p


def salva_progetto(p: dict) -> None:
    _scrivi_sessione(p)


def nome_per_file(testo: str) -> str:
    """Il nome della sessione reso adatto a un nome di file o cartella."""
    return re.sub(r'[/:\\\x00-\x1f]', "-", testo or "").strip(" .") or "Sessione"


def _lavoro(p: dict) -> Path:
    """La cartella di lavoro della sessione, senza crearla (una che non esiste se non è ancora assegnata).

    Se il percorso registrato non c'è più ma la cartella con lo stesso nome sta dentro FILE_DI_LAVORO
    (per esempio perché la cartella di Murnau è stata spostata o rinominata), si usa quella.
    """
    if not p.get("cartella_lavoro"):
        return Path("/nonesiste")
    registrata = Path(p["cartella_lavoro"])
    if not registrata.exists() and (FILE_DI_LAVORO / registrata.name).is_dir():
        return FILE_DI_LAVORO / registrata.name
    return registrata


def cartella_lavoro(p: dict) -> Path:
    """La cartella di lavoro della sessione: la assegna (dal nome della sessione) e la crea se serve."""
    if not p.get("cartella_lavoro"):
        usate = set()
        for f in SESSIONI.glob("*.json"):
            try:
                usate.add(json.loads(f.read_text()).get("cartella_lavoro"))
            except (OSError, ValueError):
                pass
        base = nome_per_file(p.get("nome"))
        c, n = FILE_DI_LAVORO / base, 2
        while c.exists() or str(c) in usate:
            c, n = FILE_DI_LAVORO / f"{base} {n}", n + 1
        p["cartella_lavoro"] = str(c)
        if _file_sessione(p["id"]).exists():  # senza cambiare la sessione aperta
            _file_sessione(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1))
    c = _lavoro(p)
    c.mkdir(parents=True, exist_ok=True)
    return c


def rinomina_cartella_lavoro(p: dict) -> None:
    """Dopo aver cambiato nome alla sessione, anche la sua cartella di lavoro prende il nuovo nome."""
    vecchia = _lavoro(p)
    nuova = FILE_DI_LAVORO / nome_per_file(p.get("nome"))
    if not vecchia.is_dir() or nuova == vecchia or nuova.exists() or vecchia.parent != FILE_DI_LAVORO:
        return
    if LAVORO.attivo() and LAVORO.sessione == p["id"]:
        return  # l'analisi in corso sta scrivendo lì: si rinomina alla prossima occasione
    vecchia.rename(nuova)
    p["cartella_lavoro"] = str(nuova)


# File che Murnau creava nella cartella dei risultati, prima delle cartelle di lavoro.
FILE_DI_MURNAU = ["lavoro", "segnalazioni", "segnalazioni.json", "revisione.json", "segnalazioni.csv",
                  "marker_avid.txt", "marker_resolve.edl", "report_murnau.html"]


def materiali_di(p: dict) -> set:
    return {str(Path(x).resolve()) for x in [p.get("video"), p.get("montaggio")]
            + [x["percorso"] for x in p.get("scartati", [])] if x}


def da_traslocare(p: dict) -> list:
    """(file nella cartella dei risultati, destinazione nella cartella di lavoro) dei file che le versioni
    precedenti di Murnau lasciavano lì. Mai un materiale; mai un file di Vertov, che può usare la stessa
    cartella e scriveva anche lui un marker_resolve.edl (il suo inizia con «TITLE: Descrizioni inquadrature»)."""
    out = Path(p.get("out") or "/nonesiste")
    if not out.is_dir() or (p.get("cartella_lavoro") and out == _lavoro(p)):
        return []
    if not (out / "segnalazioni.json").exists() and not (out / "lavoro" / "esame.jsonl").exists():
        return []  # in questa cartella Murnau non ha mai lavorato
    materiali, mosse = materiali_di(p), []
    destinazione = _lavoro(p) if p.get("cartella_lavoro") else FILE_DI_LAVORO / nome_per_file(p.get("nome"))
    for nome in FILE_DI_MURNAU:
        f = out / nome
        if not f.exists() or str(f.resolve()) in materiali:
            continue
        if nome == "marker_resolve.edl" and not f.read_text(errors="ignore").startswith("TITLE: Murnau"):
            continue  # stesso nome, ma è il file di Vertov
        mosse.append((f, destinazione / nome))
    return mosse


def trasloca(p: dict) -> list:
    """Sposta nella cartella di lavoro i file che le versioni precedenti di Murnau lasciavano nella
    cartella dei risultati. Spostati, non cancellati. La cartella dei risultati diventa quella in cui
    esportare."""
    import shutil
    mosse = da_traslocare(p)
    if not mosse:
        return []
    cartella_lavoro(p)
    spostati = []
    for f, dest in mosse:
        if dest.exists():
            continue  # c'è già una versione nella cartella di lavoro: non la si sovrascrive
        shutil.move(str(f), str(dest))
        spostati.append(f.name)
    return spostati


def riporta_dentro(p: dict) -> str:
    """Porta la cartella di lavoro della sessione dentro FILE_DI_LAVORO, se sta altrove perché creata
    da una versione precedente (in My Drive/CLAUDE o in Application Support) o perché la cartella di
    Murnau è stata spostata. Restituisce cosa è successo, o "" se non c'era niente da fare."""
    import shutil
    if not p.get("cartella_lavoro"):
        return ""
    vecchia = Path(p["cartella_lavoro"])
    if vecchia.parent == FILE_DI_LAVORO:
        return ""
    nuova = FILE_DI_LAVORO / vecchia.name
    if vecchia.is_dir() and not nuova.exists():
        FILE_DI_LAVORO.mkdir(parents=True, exist_ok=True)
        shutil.move(str(vecchia), str(nuova))
        esito = f"cartella di lavoro spostata in {nuova}"
    elif nuova.is_dir():
        esito = f"cartella di lavoro ritrovata in {nuova}"
    else:
        return ""  # né la vecchia né la nuova: la sessione non ha ancora lavorato
    p["cartella_lavoro"] = str(nuova)
    # La cartella che conteneva le cartelle di lavoro, se ora è vuota, non serve più.
    try:
        rimasti = [x for x in vecchia.parent.iterdir() if x.name != ".DS_Store"]
        if vecchia.parent.name == NOME_FILE_DI_LAVORO and not rimasti:
            shutil.rmtree(vecchia.parent)
    except OSError:
        pass
    return esito


def trasloca_tutte() -> None:
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
            esito = riporta_dentro(p)
            if esito:
                f.write_text(json.dumps(p, ensure_ascii=False, indent=1))
                print(f"Sessione {p.get('nome')}: {esito}")
        except Exception as e:
            print(f"Sessione {f.stem}: cartella di lavoro non spostata ({e})")
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
            spostati = trasloca(p)
            if spostati:
                f.write_text(json.dumps(p, ensure_ascii=False, indent=1))
                print(f"Sessione {p.get('nome')}: spostati nella cartella di lavoro {', '.join(spostati)}")
        except Exception as e:
            print(f"Sessione {f.stem}: file di lavoro non spostati ({e})")


def _nel_cestino(percorso: Path) -> None:
    """Sposta nel Cestino di macOS (recuperabile), senza cancellare definitivamente."""
    from Foundation import NSFileManager, NSURL
    ok, _, errore = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(percorso)), None, None)
    if not ok:
        raise RuntimeError(f"Non riesco a spostare nel Cestino {percorso.name}: {errore}")


def elimina_sessione(id_: str, risultati: bool) -> dict:
    """Elimina una sessione da Murnau. Il film e il montaggio non vengono mai toccati; i risultati
    vanno nel Cestino solo se richiesto e se nessun'altra sessione li usa."""
    if LAVORO.attivo() and LAVORO.sessione == id_:
        raise RuntimeError("Questa sessione ha un'analisi in corso: fermala prima di eliminarla.")
    p = json.loads(_file_sessione(id_).read_text())
    altre = [json.loads(f.read_text()) for f in SESSIONI.glob("*.json") if f.stem != id_]
    spostati = []
    if risultati:
        materiali = materiali_di(p)
        if _lavoro(p).is_dir():
            _nel_cestino(_lavoro(p))
            spostati.append(_lavoro(p).name)
        for x in p.get("esportati", []):
            f = Path(x)
            if f.exists() and str(f.resolve()) not in materiali:  # mai un file scelto come materiale
                _nel_cestino(f)
                spostati.append(f.name)
        out = Path(p.get("out") or "/nonesiste")
        if out.is_dir() and not any(a.get("out") == p["out"] for a in altre) and \
                not any(x for x in out.iterdir() if x.name != ".DS_Store"):
            _nel_cestino(out)  # la cartella era solo di Murnau ed è rimasta vuota
    _file_sessione(id_).unlink()
    # Se era la sessione aperta, si passa alla più recente (o a una nuova).
    try:
        corrente = json.loads(FILE_CORRENTE.read_text()).get("id")
    except (OSError, ValueError):
        corrente = None
    if corrente == id_ or corrente is None:
        rimaste = elenco_sessioni()
        prossima = json.loads(_file_sessione(rimaste[0]["id"]).read_text()) if rimaste else nuova_sessione()
        _scrivi_sessione(prossima)
    return {"spostati": spostati}


def elenco_sessioni() -> list:
    elenco = []
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        segnalazioni = None
        if (_lavoro(p) / "segnalazioni.json").exists():
            try:
                segnalazioni = len(json.loads((_lavoro(p) / "segnalazioni.json").read_text())["segnalazioni"])
            except (OSError, ValueError, KeyError):
                pass
        elenco.append({"id": p["id"], "nome": p.get("nome", ""), "modificata": p.get("modificata", ""),
                       "video": Path(p["video"]).name if p.get("video") else "", "segnalazioni": segnalazioni})
    return sorted(elenco, key=lambda e: e["modificata"], reverse=True)


# --------------------------------------------------------------------------
# Materiali: riconoscimento e assegnazione
# --------------------------------------------------------------------------

def _durata(sec: float) -> str:
    sec = round(sec)
    return f"{sec // 3600} h {sec // 60 % 60:02d} min" if sec >= 3600 else f"{sec // 60} min {sec % 60:02d} s"


def descrivi_video(percorso: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,width,height,r_frame_rate:format=duration:"
                          "stream_tags=timecode:format_tags=timecode", "-of", "json", str(percorso)],
                         capture_output=True, text=True, timeout=60)
    dati = json.loads(out.stdout or "{}")
    if not dati.get("streams"):
        raise ValueError("non contiene video leggibile")
    s = dati["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    durata = float(dati.get("format", {}).get("duration") or 0)
    tc = s.get("tags", {}).get("timecode") or dati.get("format", {}).get("tags", {}).get("timecode")
    parti = [_durata(durata), f"{fps:g} fps", f"{s.get('width')}×{s.get('height')}", (s.get("codec_name") or "").upper()]
    if tc:
        parti.append(f"inizia a {tc}")
    return {"durata": durata, "dettaglio": " · ".join(x for x in parti if x)}


def descrivi_montaggio(percorso: Path) -> dict:
    if percorso.suffix.lower() == ".aaf":
        return {"dettaglio": f"AAF · {percorso.stat().st_size / 2**20:.1f} MB · tagli, clip e metadati del bin"}
    eventi = [e for e in montaggio.leggi_edl(percorso) if e["video"]]
    if not eventi:
        raise ValueError("non contiene eventi video")
    return {"dettaglio": f"EDL · {len(eventi)} eventi video · da {eventi[0]['tc_in']}"}


def riconosci(percorsi: list) -> list:
    """Per ogni file o cartella: {percorso, nome, ruolo o None, dettaglio, problema}."""
    file = []
    for x in percorsi:
        x = Path(x.rstrip("/"))
        if x.is_dir():
            if (x / "segnalazioni.json").exists():
                file.append(x)
            else:
                file += sorted(f for f in x.iterdir() if f.suffix.lower() in ESTENSIONI_VIDEO | ESTENSIONI_MONTAGGIO)
        else:
            file.append(x)
    voci = []
    for f in file:
        v = {"percorso": str(f), "nome": f.name, "ruolo": None, "dettaglio": "", "problema": ""}
        try:
            if f.is_dir():
                v.update(ruolo="risultati", dettaglio="analisi già fatta da Murnau")
            elif f.suffix.lower() in ESTENSIONI_VIDEO:
                v.update(ruolo="video", **descrivi_video(f))
            elif f.suffix.lower() in ESTENSIONI_MONTAGGIO:
                v.update(ruolo="montaggio", **descrivi_montaggio(f))
            else:
                v["problema"] = "Formato non usato da Murnau: servono il film (.mov, .mxf, .mp4) e l'EDL o l'AAF."
        except Exception as e:
            v["problema"] = f"Non riesco a leggerlo: {e}."
        voci.append(v)
    # Più film o più montaggi insieme: si usa il film più lungo e il primo montaggio.
    for ruolo, chiave in (("video", lambda v: -v.get("durata", 0)), ("montaggio", lambda v: 0)):
        stessi = sorted((v for v in voci if v["ruolo"] == ruolo), key=chiave)
        for v in stessi[1:]:
            v["ruolo"], v["problema"] = None, f"Non usato: come {RUOLI[ruolo].lower()} uso {stessi[0]['nome']}."
    return voci


def togli_materiale(p: dict, percorso: str) -> None:
    for ruolo in RUOLI:
        if p.get(ruolo) == percorso:
            p[ruolo] = ""
    p["scartati"] = [x for x in p.get("scartati", []) if x["percorso"] != percorso]


def assegna(p: dict, percorso: str, ruolo: str) -> None:
    """Mette un file in un ruolo, togliendolo da quello che aveva prima."""
    togli_materiale(p, percorso)
    if ruolo == "risultati":
        # Un'analisi già fatta (cartella con segnalazioni.json): diventa la cartella di lavoro.
        p["cartella_lavoro"] = percorso
        return
    p[ruolo] = percorso
    if ruolo == "video":
        video = Path(percorso)
        if not p.get("out"):
            p["out"] = str(video.parent)  # le esportazioni, se non si sceglie altro, accanto al film
        if p.get("nome") in ("", "Nuova sessione"):
            p["nome"] = video.stem
        cartella_lavoro(p)


def aggiungi_materiali(p: dict, percorsi: list) -> list:
    voci = riconosci(percorsi)
    info = p.setdefault("info", {})
    for v in voci:
        if v["ruolo"]:
            if v["ruolo"] in RUOLI and p.get(v["ruolo"]) and p[v["ruolo"]] != v["percorso"]:
                info.pop(p[v["ruolo"]], None)  # il file sostituito non serve più
            assegna(p, v["percorso"], v["ruolo"])
            info[v["percorso"]] = v["dettaglio"]
        else:
            togli_materiale(p, v["percorso"])
            p.setdefault("scartati", []).append({k: v[k] for k in ("percorso", "nome", "problema")})
    salva_progetto(p)
    return voci


def comando(p: dict) -> list:
    cmd = [sys.executable, "-W", "ignore", str(CARTELLA / "murnau.py"), p["video"], "--out", str(cartella_lavoro(p)),
           "--categorie", ",".join(p.get("categorie") or rilevatori.CATEGORIE)]
    if p.get("montaggio"):
        cmd += ["--montaggio", p["montaggio"]]
    if p.get("tc_inizio", "").strip():
        cmd += ["--tc-inizio", p["tc_inizio"].strip()]
    if p.get("accurato"):
        cmd.append("--accurato")
    return cmd


# --------------------------------------------------------------------------
# Analisi in corso (una sola alla volta)
# --------------------------------------------------------------------------

class Lavoro:
    def __init__(self):
        self.processo = None
        self.log = deque(maxlen=300)
        self.fase = ""
        self.avanz = {}
        self.piano = {}       # fase -> (unità di lavoro, secondi stimati per unità), nell'ordine delle fasi
        try:
            self.velocita = json.loads(FILE_VELOCITA.read_text())
        except (OSError, ValueError):
            self.velocita = {}
        self.inizio = 0.0
        self.inizio_fase = 0.0
        self.mostrata = 0     # percentuale già mostrata: non deve mai tornare indietro
        self.attesa = ""      # perché l'analisi è ferma in attesa (es. Vertov usa il modello)
        self.avvisi = []      # cose da verificare segnalate durante il lavoro (righe "@@avviso")
        self.fermata = False  # fermata dall'utente (Ferma o chiusura della finestra), non un errore
        self.esito = None     # com'è andata, da mostrare finché l'utente non preme OK
        self.codice = None
        self.sessione = ""
        self.blocco = threading.Lock()

    def attivo(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    def avvia(self, cmd: list, sessione: str) -> None:
        with self.blocco:
            if self.attivo():
                raise RuntimeError("C'è già un'analisi in corso.")
            fuori = coordinamento.analisi_in_corso()
            if fuori:
                raise RuntimeError("C'è già un'analisi di Murnau in corso, avviata fuori dall'app: due analisi "
                                   "insieme si rubano il modello a vicenda. Fermala o aspetta che finisca.")
            self.fase, self.avanz, self.piano, self.codice, self.sessione = "avvio", {}, {}, None, sessione
            self.mostrata = 0
            self.attesa = ""
            self.avvisi, self.fermata, self.esito = [], False, None
            self.log.clear()
            self.inizio = time.time()
            self.processo = subprocess.Popen(
                cmd, cwd=CARTELLA, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
                env={**os.environ, "PYTHONWARNINGS": "ignore", "TQDM_DISABLE": "1",
                     "HF_HUB_DISABLE_TELEMETRY": "1",
                     # l'analisi si spegne da sola se l'app non c'è più (chiusa, forzata, in crash)
                     coordinamento.VARIABILE_GENITORE: str(os.getpid())})
        threading.Thread(target=self._leggi, daemon=True).start()

    def _leggi(self) -> None:
        for riga in self.processo.stdout:
            riga = riga.rstrip()
            if riga.startswith("@@fase "):
                self._impara()
                self.fase = riga.split(" ", 1)[1]
                self.inizio_fase = time.time()
            elif riga.startswith("@@attesa"):
                self.attesa = riga[len("@@attesa"):].strip()
            elif riga.startswith("@@avviso "):
                self.avvisi.append(riga.split(" ", 1)[1])
                self.log.append(riga.split(" ", 1)[1])
            elif riga.startswith("@@piano "):
                # Le fasi non ripetute restano come annunciate prima (es. i fotogrammi già estratti).
                for voce in riga.split()[1:]:
                    nome, valori = voce.split("=")
                    unita, costo = valori.split(":")
                    self.piano[nome] = (int(unita), float(costo))
            elif riga.startswith("@@avanz "):
                _, nome, fatti, totale = riga.split()
                primo = self.avanz.get(nome, {}).get("primo", (int(fatti), time.time()))
                self.avanz[nome] = {"fatti": int(fatti), "totale": int(totale), "primo": primo, "ora": time.time()}
            elif riga and "warn" not in riga.lower() and "%|" not in riga and "NOTE: Redirects" not in riga:
                self.log.append(riga)
        self.codice = self.processo.wait()
        self._impara()
        if not self.fermata:
            self.esito = esito_del_lavoro(self)
            avvisa_fine(self.esito)

    def _misura(self, nome: str):
        """Secondi per unità della fase, misurati in questa analisi (None se non ancora misurabili)."""
        a = self.avanz.get(nome)
        if a and a["fatti"] > a["primo"][0]:
            return (a["ora"] - a["primo"][1]) / (a["fatti"] - a["primo"][0])
        return None

    def _impara(self) -> None:
        """Ricorda la velocità della fase appena finita, per stimare meglio le prossime analisi.

        Media pesata con le volte precedenti: una sola analisi rallentata (per esempio perché
        un'altra app usava Ollama nello stesso momento) non sposta troppo la stima.
        """
        misura = self._misura(self.fase)
        if misura is None or self.avanz[self.fase]["fatti"] < 3:
            return
        prima = self.velocita.get(self.fase)
        self.velocita[self.fase] = misura if prima is None else .6 * prima + .4 * misura
        try:
            DATI.mkdir(parents=True, exist_ok=True)
            FILE_VELOCITA.write_text(json.dumps(self.velocita, indent=1))
        except OSError:
            pass

    def ferma(self) -> None:
        """Ferma l'analisi e tutto ciò che ha avviato; se entro 5 secondi non è uscita, la chiude d'ufficio."""
        if not self.attivo():
            return
        processo = self.processo
        self.fermata = True  # così l'uscita con il segnale di arresto non sembra un errore
        try:
            os.killpg(processo.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(processo.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def complessivo(self) -> dict:
        """Percentuale e tempo restante dell'intera analisi.

        Ogni fase pesa per il suo lavoro (unità × secondi per unità). I secondi per unità sono
        quelli misurati mentre la fase procede; per le fasi non ancora iniziate quelli imparati
        nelle analisi precedenti su questo Mac, o in mancanza la stima del piano. La velocità di
        una fase non dice nulla sulle altre: OWLv2 e Qwen3-VL lavorano in modo troppo diverso.
        """
        if self.fase == "fine":
            return {"percentuale": 100, "eta": 0}
        if not self.piano:
            return {"percentuale": 0, "eta": None}
        fasi = list(self.piano)
        indice = fasi.index(self.fase) if self.fase in fasi else -1
        misure = {n: m for n in fasi if (m := self._misura(n)) is not None}
        totale = fatto = 0.0
        for i, nome in enumerate(fasi):
            unita, costo = self.piano[nome]
            a = self.avanz.get(nome)
            if a:
                unita = a["totale"]
            per_unita = misure.get(nome, self.velocita.get(nome, costo))
            if i < indice:
                svolte = unita
            elif i == indice:
                svolte = a["fatti"] if a else 0
            else:
                svolte = 0
            totale += unita * per_unita
            fatto += svolte * per_unita
        if self.fase in fasi:
            # Tra un aggiornamento e l'altro della fase in corso il tempo passa comunque: fino al
            # tempo atteso per un'unità conta come lavoro svolto; oltre, è un ritardo (per esempio
            # un'altra app che usa Ollama nello stesso momento): allunga il tempo restante ma non
            # fa tornare indietro la percentuale.
            a = self.avanz.get(self.fase) or {"ora": self.inizio_fase}
            atteso = misure.get(self.fase, self.velocita.get(self.fase, self.piano[self.fase][1]))
            passato = time.time() - a["ora"]
            fatto = min(totale, fatto + min(passato, atteso))
            ritardo = max(0.0, passato - atteso)
        else:
            ritardo = 0.0
        if totale <= 0:
            return {"percentuale": 0, "eta": None}
        # Se una fase si rivela più lenta del previsto il totale cresce: il tempo restante si
        # aggiorna, ma la percentuale mostrata resta ferma finché il lavoro non la raggiunge.
        self.mostrata = max(self.mostrata, min(99, int(100 * fatto / totale)))
        return {"percentuale": self.mostrata, "eta": max(0.0, totale - fatto) + ritardo}

    def stato(self) -> dict:
        avanz = {}
        for nome, a in self.avanz.items():
            fatti0, t0 = a["primo"]
            eta = None
            if fatti0 < a["fatti"] < a["totale"]:
                eta = (a["ora"] - t0) / (a["fatti"] - fatti0) * (a["totale"] - a["fatti"])
            avanz[nome] = {"fatti": a["fatti"], "totale": a["totale"], "eta": eta}
        return {"attivo": self.attivo(), "fase": self.fase, "avanzamento": avanz, "codice": self.codice,
                "complessivo": self.complessivo(), "attesa": self.attesa if self.attivo() else "",
                "fermata": self.fermata, "esito": self.esito,
                "sessione": self.sessione, "durata": time.time() - self.inizio if self.inizio else 0,
                "log": list(self.log)[-40:]}


LAVORO = Lavoro()


def _ultimo_errore(log: list) -> str:
    """La riga che spiega l'errore: di solito l'ultima di un traceback di Python."""
    righe = [r.strip() for r in log if r.strip() and not r.startswith("@@")]
    riga = next((r for r in reversed(righe) if re.match(r"^[A-Za-z_.]*(Error|Exception)\b", r)),
                righe[-1] if righe else "")
    # Gli errori più comuni, detti in parole semplici (il testo tecnico resta nei dettagli).
    if m := re.search(r"Command \['(ffprobe|ffmpeg)'.*'([^']+)'\] returned non-zero", riga):
        return f"ffmpeg non riesce a leggere {Path(m.group(2)).name}: il file è danneggiato o non è un video."
    if "ffmpeg non è riuscito a leggere il video" in riga:
        return "ffmpeg non riesce a leggere il film: il file è danneggiato, incompleto o non è un video."
    if m := re.match(r"FileNotFoundError: .*?'([^']+)'", riga):
        return f"Non trovo {Path(m.group(1)).name}: è stato spostato o il disco è scollegato."
    if "Connection refused" in riga or "URLError" in riga or "Ollama non è acceso" in riga:
        return "Ollama non risponde: forse si è chiuso. Riaprilo (o riavvia il Mac) e rilancia."
    if "Il montaggio non corrisponde al video" in riga:
        return "Il montaggio (EDL o AAF) non corrisponde al film: forse sono di versioni diverse, o il " \
               "timecode di inizio del film è sbagliato."
    if "No space left" in riga:
        return "Il disco è pieno: libera spazio (anche con «Libera spazio…») e rilancia."
    return re.sub(r"^[A-Za-z_.]*(Error|Exception):\s*", "", riga)


def esito_del_lavoro(l) -> dict:
    """Com'è andata un'analisi finita da sola: senza errori, con cose da verificare o interrotta."""
    if l.codice != 0:
        motivo = _ultimo_errore(list(l.log))
        return {"stato": "errore", "titolo": "L'analisi si è interrotta per un errore",
                "punti": ([f"Motivo: {motivo}"] if motivo else []) +
                         ["Il lavoro fatto fin qui è salvato: rilanciandola riprende da dove era arrivata.",
                          "Il testo tecnico è in «Dettagli dell'elaborazione»."], "segnalazioni": None}
    try:
        p = json.loads(_file_sessione(l.sessione).read_text())
        segnalazioni = len(json.loads((_lavoro(p) / "segnalazioni.json").read_text())["segnalazioni"])
    except (OSError, ValueError, KeyError):
        segnalazioni = None
    punti = list(dict.fromkeys(l.avvisi))
    return {"stato": "verifica" if punti else "ok", "titolo": "Analisi del film completata", "punti": punti,
            "segnalazioni": segnalazioni}


def avvisa_fine(esito: dict) -> None:
    """Suono e notifica di macOS quando un'analisi finisce da sola (non se l'ha fermata l'utente).
    Nella finestra di Murnau compare poi il riquadro con l'esito, da chiudere con OK."""
    suono = "/System/Library/Sounds/" + ("Basso.aiff" if esito["stato"] == "errore" else "Glass.aiff")
    testo = esito["titolo"] + (": ci sono cose da verificare." if esito["stato"] == "verifica" else ".")
    subprocess.Popen(["afplay", suono])
    subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(testo)} with title "Murnau"'])


# --------------------------------------------------------------------------
# Segnalazioni e revisione
# --------------------------------------------------------------------------

def leggi_segnalazioni(out: Path) -> dict:
    if not (out / "segnalazioni.json").exists():
        return {"segnalazioni": [], "revisione": {}}
    dati, revisione = esporta.leggi(out)
    return {**dati, "revisione": revisione}


def rivedi(out: Path, ident: str, stato: str = None, nota: str = None) -> None:
    _, revisione = esporta.leggi(out)
    if stato is not None:
        if stato:
            revisione[ident] = stato
        else:
            revisione.pop(ident, None)
    if nota is not None:
        if nota.strip():
            revisione["note:" + ident] = nota.strip()
        else:
            revisione.pop("note:" + ident, None)
    (out / "revisione.json").write_text(json.dumps(revisione, ensure_ascii=False, indent=1), encoding="utf-8")
    esporta.tutto(out)


# Cosa si può esportare: file nella cartella di lavoro e come si chiama, una volta esportato,
# dopo il nome della sessione.
ESPORTAZIONI = {
    "csv": ("segnalazioni.csv", "segnalazioni.csv"),
    "avid": ("marker_avid.txt", "marker Avid.txt"),
    "resolve": ("marker_resolve.edl", "marker Resolve.edl"),
    "report": ("report_murnau.html", "report.html"),
}


def esporta_file(p: dict, formati: list) -> list:
    """Copia nella cartella di destinazione i formati scelti, con il nome della sessione."""
    import shutil
    if not formati:
        raise RuntimeError("Scegli almeno un tipo di file da esportare.")
    if not p.get("out"):
        raise RuntimeError("Scegli prima la cartella in cui esportare.")
    lavoro = _lavoro(p)
    if not (lavoro / "segnalazioni.json").exists():
        raise RuntimeError("Non c'è ancora niente da esportare: prima analizza il film.")
    esporta.tutto(lavoro)  # i file della cartella di lavoro, aggiornati con l'ultima revisione
    dest = Path(p["out"])
    dest.mkdir(parents=True, exist_ok=True)
    materiali, creati = materiali_di(p), []
    for f in formati:
        sorgente, suffisso = ESPORTAZIONI[f]
        file = dest / f"{nome_per_file(p.get('nome'))} - {suffisso}"
        if str(file.resolve()) in materiali:  # mai sovrascrivere un materiale
            raise RuntimeError(f"{file.name} è uno dei materiali della sessione: scegli un'altra cartella.")
        if f == "report":
            # Il report esportato porta con sé le immagini: si apre anche lontano dalla cartella di lavoro.
            dati, revisione = esporta.leggi(lavoro)
            esporta.scrivi_report(lavoro, dati, revisione, destinazione=file)
        else:
            shutil.copyfile(lavoro / sorgente, file)
        creati.append(str(file))
    return creati


FINESTRA = None  # la finestra dell'app, per le finestre di scelta dei file


def scegli(tipo: str, multipli: bool = False):
    """Finestra di scelta nativa: un percorso, una lista (multipli) o None se annullata."""
    import webview
    dialogo = getattr(webview, "FileDialog", None)
    if dialogo:
        modo = dialogo.FOLDER if tipo == "cartella" else dialogo.OPEN
    else:
        modo = webview.FOLDER_DIALOG if tipo == "cartella" else webview.OPEN_DIALOG
    scelti = FINESTRA.create_file_dialog(modo, allow_multiple=multipli and tipo != "cartella")
    if not scelti:
        return None
    scelti = [x.rstrip("/") for x in (list(scelti) if isinstance(scelti, (list, tuple)) else [scelti])]
    return scelti if multipli else scelti[0]


class Gestore(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _invia(self, corpo: bytes, tipo: str, codice: int = 200):
        self.send_response(codice)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dati, codice=200):
        self._invia(json.dumps(dati, ensure_ascii=False).encode(), "application/json; charset=utf-8", codice)

    def _corpo(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        p = carica_progetto()
        out = _lavoro(p)
        if url.path == "/":
            self._invia((CARTELLA / "interfaccia.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/aiuto":
            self._invia((CARTELLA / "aiuto.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/icona.png":
            self._invia((CARTELLA / "icona_murnau.png").read_bytes(), "image/png")
        elif url.path == "/api/stato":
            mancanti = [p[r] for r in RUOLI if p.get(r) and not Path(p[r]).exists()]
            nostra = LAVORO.processo.pid if LAVORO.attivo() else None
            esterne = []
            for a in coordinamento.analisi_in_corso():
                if a["pid"] == nostra:
                    continue
                esterne.append({"pid": a["pid"], "film": Path(a["video"]).name if a.get("video") else "",
                                "origine": a.get("origine", "sconosciuta"),
                                "da": time.time() - a["avviata"] if a.get("avviata") else None,
                                "fase": a.get("fase", ""), "fatti": a.get("fatti", 0), "totale": a.get("totale", 0),
                                "attesa": a.get("attesa", ""), "out": a.get("out", "")})
            self._json({"progetto": p, "lavoro": LAVORO.stato(), "mancanti": mancanti, "esterne": esterne,
                        "ollama": rilevatori.ollama_pronto(""), "modello": rilevatori.ollama_pronto(MODELLO),
                        "risultati": (out / "segnalazioni.json").exists(),
                        "esportabili": (out / "segnalazioni.json").exists(),
                        "categorie": rilevatori.CATEGORIE})
        elif url.path == "/api/sessioni":
            self._json({"corrente": p["id"], "sessioni": elenco_sessioni()})
        elif url.path == "/api/segnalazioni":
            self._json(leggi_segnalazioni(out))
        elif url.path == "/api/aggiornamenti":
            self._json({"installazione": aggiornamenti.INSTALLAZIONE, "versione": aggiornamenti.versione(),
                        "automatico": aggiornamenti.leggi_stato().get("automatico", True)})
        elif url.path == "/api/pulizia":
            self._json({"voci": pulizia.voci(MODELLO)})
        elif url.path == "/file":
            # Solo le immagini dentro la cartella di lavoro.
            relativo = parse_qs(url.query).get("p", [""])[0]
            percorso = (out / relativo).resolve()
            if out.resolve() in percorso.parents and percorso.suffix.lower() in TIPI_IMMAGINE and percorso.exists():
                self._invia(percorso.read_bytes(), TIPI_IMMAGINE[percorso.suffix.lower()])
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            corpo = self._corpo()
            p = carica_progetto()
            if url.path == "/api/progetto":
                for chiave in ("categorie", "accurato", "tc_inizio", "nome"):
                    if chiave in corpo:
                        p[chiave] = corpo[chiave]
                salva_progetto(p)
                self._json({"progetto": p})
            elif url.path == "/api/sessione":
                azione = corpo.get("azione")
                if azione in ("nuova", "apri") and LAVORO.attivo():
                    raise RuntimeError("C'è un'analisi in corso: fermala prima di cambiare sessione.")
                if azione == "nuova":
                    p = nuova_sessione(corpo.get("nome", ""))
                elif azione == "apri":
                    p = json.loads(_file_sessione(corpo["id"]).read_text())
                    _scrivi_sessione(p)
                elif azione == "elimina":
                    esito = elimina_sessione(corpo["id"], bool(corpo.get("risultati")))
                    self._json({"ok": True, "progetto": carica_progetto(), **esito})
                    return
                elif azione == "salva":
                    if corpo.get("nome", "").strip():
                        p["nome"] = corpo["nome"].strip()
                        rinomina_cartella_lavoro(p)
                    salva_progetto(p)
                self._json({"ok": True, "progetto": p})
            elif url.path == "/api/sfoglia":
                # Finestra di scelta: i file scelti vengono riconosciuti come quelli trascinati.
                scelti = scegli(corpo.get("tipo", "file"), multipli=True)
                if not scelti:
                    self._json({"esiti": [], "progetto": p})
                    return
                self._json({"esiti": aggiungi_materiali(p, scelti), "progetto": p})
            elif url.path == "/api/aggiungi":
                self._json({"esiti": aggiungi_materiali(p, corpo.get("percorsi", [])), "progetto": p})
            elif url.path == "/api/ruolo":
                if corpo.get("ruolo"):
                    assegna(p, corpo["percorso"], corpo["ruolo"])
                else:
                    togli_materiale(p, corpo["percorso"])
                    p.get("info", {}).pop(corpo["percorso"], None)
                salva_progetto(p)
                self._json({"ok": True, "progetto": p})
            elif url.path == "/api/cartella-esporta":
                scelto = scegli("cartella")
                if scelto:
                    p["out"] = scelto
                    salva_progetto(p)
                self._json({"progetto": p})
            elif url.path == "/api/avvia":
                if not p.get("video"):
                    raise RuntimeError("Scegli prima il film da analizzare.")
                if not Path(p["video"]).exists():
                    raise RuntimeError("Il film non si trova più: trascinalo di nuovo nei materiali.")
                if not p.get("categorie"):
                    raise RuntimeError("Scegli almeno un tipo di errore da cercare.")
                LAVORO.avvia(comando(p), p["id"])
                self._json({"ok": True})
            elif url.path == "/api/ferma":
                LAVORO.ferma()
                self._json({"ok": True})
            elif url.path == "/api/ferma-esterna":
                pid = int(corpo["pid"])
                if not coordinamento.e_murnau(pid):  # mai fermare un processo che non sia un'analisi di Murnau
                    raise RuntimeError("Quell'analisi non è più in corso.")
                os.kill(pid, signal.SIGTERM)
                self._json({"ok": True})
            elif url.path == "/api/rivedi":
                rivedi(_lavoro(p), corpo["id"], corpo.get("stato"), corpo.get("nota"))
                self._json({"ok": True})
            elif url.path == "/api/aggiornamenti":
                azione = corpo.get("azione")
                if azione == "controlla":
                    manuale = bool(corpo.get("manuale"))
                    if not manuale and not aggiornamenti.da_controllare():
                        self._json({"da_fare": False})
                    else:
                        self._json(aggiornamenti.controlla(MODELLO, manuale))
                elif azione == "installa":
                    if LAVORO.attivo():
                        raise RuntimeError("Aspetta che finisca l'analisi in corso, o fermala, prima di aggiornare.")
                    aggiornamenti.installa(corpo.get("voci") or [], MODELLO)
                    self._json({"ok": True})
                elif azione == "salta":
                    aggiornamenti.salta(corpo.get("voci") or [])
                    self._json({"ok": True})
                elif azione == "automatico":
                    aggiornamenti.scrivi_stato(automatico=bool(corpo.get("valore")))
                    self._json({"ok": True})
                elif azione == "riavvia":
                    self._json({"ok": True})
                    aggiornamenti.riavvia(PORTA)
                    threading.Timer(0.3, FINESTRA.destroy).start()
                else:
                    self.send_error(404)
            elif url.path == "/api/pulizia":
                if LAVORO.attivo() or aggiornamenti.INSTALLAZIONE["attiva"]:
                    raise RuntimeError("Aspetta che finisca il lavoro in corso prima di liberare spazio.")
                self._json({"liberati": pulizia.cancella(corpo.get("chiavi") or [], MODELLO)})
            elif url.path == "/api/apri":
                cosa = corpo.get("cosa")
                if cosa == "registro":
                    subprocess.run(["open", "-a", "Console", str(REGISTRO)])
                    self._json({"ok": True})
                    return
                # La cartella di lavoro, il report (quello della cartella di lavoro, con le immagini
                # accanto) o la cartella di destinazione con selezionati i file appena esportati.
                creati = [x for x in corpo.get("file", []) if x in p.get("esportati", []) and Path(x).exists()]
                if cosa == "lavoro":
                    subprocess.run(["open", str(cartella_lavoro(p))])
                elif cosa == "report":
                    if not (_lavoro(p) / "report_murnau.html").exists():
                        raise RuntimeError("Il report non c'è ancora: viene creato alla fine dell'analisi.")
                    subprocess.run(["open", str(_lavoro(p) / "report_murnau.html")])
                elif creati:
                    subprocess.run(["open", "-R", *creati])
                elif p.get("out") and Path(p["out"]).is_dir():
                    subprocess.run(["open", p["out"]])
                else:
                    raise RuntimeError("La cartella di destinazione non esiste.")
                self._json({"ok": True})
            elif url.path == "/api/esporta":
                if corpo.get("cartella"):
                    p["out"] = corpo["cartella"]
                p["formati"] = [f for f in corpo.get("formati", []) if f in ESPORTAZIONI]
                creati = esporta_file(p, p["formati"])
                p["esportati"] = sorted(set(p.get("esportati", [])) | set(creati))
                salva_progetto(p)
                self._json({"ok": True, "creati": creati, "cartella": p["out"]})
            elif url.path == "/api/esito-visto":
                LAVORO.esito = None
                self._json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"errore": str(e)}, 400)


# --------------------------------------------------------------------------
# Finestra
# --------------------------------------------------------------------------

def identita_mac() -> None:
    """Nome e icona di Murnau nel Dock e nella barra dei menu (invece di "Python")."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Murnau"
        icona = NSImage.alloc().initWithContentsOfFile_(str(CARTELLA / "icona_murnau.png"))
        if icona:
            NSApplication.sharedApplication().setApplicationIconImage_(icona)
    except Exception:
        pass


def segui_nel_dock() -> None:
    """Durante un'analisi, la percentuale compare sull'icona di Murnau nel Dock."""
    from AppKit import NSApplication
    from PyObjCTools import AppHelper

    def metti(testo):
        NSApplication.sharedApplication().dockTile().setBadgeLabel_(testo)

    ultimo = None
    while True:
        testo = f"{LAVORO.complessivo()['percentuale']}%" if LAVORO.attivo() else ""
        if testo != ultimo:
            AppHelper.callAfter(metti, testo)
            ultimo = testo
        time.sleep(3)


def collega_trascinamento(finestra) -> None:
    """File e cartelle lasciati sulla finestra: i percorsi completi arrivano dal lato nativo."""
    from webview.dom import DOMEventHandler

    def lasciati(evento):
        percorsi = [f.get("pywebviewFullPath") for f in evento.get("dataTransfer", {}).get("files", [])]
        percorsi = [x for x in percorsi if x]
        if not percorsi:
            return
        finestra.evaluate_js(f"materialiInArrivo({len(percorsi)})")
        try:
            esiti = aggiungi_materiali(carica_progetto(), percorsi)
        except Exception as e:
            esiti = [{"nome": "", "ruolo": None, "problema": str(e)}]
        finestra.evaluate_js(f"materialiAggiunti({json.dumps(esiti, ensure_ascii=False)})")

    finestra.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True, debounce=500)
    finestra.dom.document.events.drop += DOMEventHandler(lasciati, True, True)


# --------------------------------------------------------------------------
# Menu della barra di macOS
# --------------------------------------------------------------------------

def _js(comando: str):
    """Azione di menu che esegue un comando dell'interfaccia (murnauComando in interfaccia.html)."""
    def azione():
        if FINESTRA:
            FINESTRA.evaluate_js(f"murnauComando({json.dumps(comando)})")
    azione.__name__ = "menu_" + re.sub(r"\W", "_", comando)
    return azione


def apri_aiuto():
    import webview
    webview.create_window("Guida di Murnau", f"http://127.0.0.1:{PORTA}/aiuto", width=820, height=820,
                          background_color="#16161a", text_select=True)


def apri_registro():
    subprocess.run(["open", "-a", "Console", str(REGISTRO)] if REGISTRO.exists() else ["open", str(DATI)])


def crea_menu() -> list:
    from webview.menu import Menu, MenuAction, MenuSeparator
    return [
        Menu("File", [
            MenuAction("Nuova sessione", _js("nuova")),
            MenuAction("Apri sessione…", _js("apri")),
            MenuAction("Salva sessione", _js("salva")),
            MenuAction("Elimina sessione…", _js("elimina")),
            MenuSeparator(),
            MenuAction("Aggiungi materiali…", _js("aggiungi-file")),
            MenuAction("Aggiungi una cartella…", _js("aggiungi-cartella")),
            MenuSeparator(),
            MenuAction("Apri il report", _js("report")),
            MenuAction("Esporta…", _js("esporta")),
            MenuAction("Mostra i file di lavoro", _js("lavoro")),
        ]),
        Menu("Vista", [
            MenuAction("Analisi", _js("vista-analisi")),
            MenuAction("Segnalazioni", _js("vista-segnalazioni")),
            MenuAction("Esporta", _js("vista-esporta")),
            MenuSeparator(),
            MenuAction("Dettagli dell'elaborazione", _js("dettagli")),
        ]),
        Menu("Analisi", [
            MenuAction("Analizza il film", _js("analizza")),
            MenuAction("Ferma", _js("ferma")),
        ]),
        Menu("Aiuto", [
            MenuAction("Guida di Murnau", apri_aiuto),
            MenuSeparator(),
            MenuAction("Cerca aggiornamenti…", _js("aggiornamenti")),
            MenuAction("Libera spazio…", _js("pulizia")),
            MenuSeparator(),
            MenuAction("Apri il registro di Murnau", apri_registro),
        ]),
    ]


# Scorciatoie da tastiera dei menu: titolo -> (tasto, maiuscole)
SCORCIATOIE = {
    "Nuova sessione": ("n", False), "Apri sessione…": ("o", False), "Salva sessione": ("s", False),
    "Aggiungi materiali…": ("o", True), "Esporta…": ("e", False), "Analisi": ("1", False), "Segnalazioni": ("2", False), "Esporta": ("3", False),
    "Analizza il film": ("r", False), "Ferma": (".", False), "Guida di Murnau": ("?", False),
}


def sistema_menu() -> None:
    """Completa la barra dei menu con le parti native: Modifica, Finestra, scorciatoie."""
    import AppKit
    app = AppKit.NSApplication.sharedApplication()
    barra = app.mainMenu()
    if barra is None:
        return

    def menu_nativo(titolo, voci):
        menu = AppKit.NSMenu.alloc().initWithTitle_(titolo)
        for voce in voci:
            if voce is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            nome, azione, tasto, *mod = voce
            item = menu.addItemWithTitle_action_keyEquivalent_(nome, azione, tasto)
            if mod:
                item.setKeyEquivalentModifierMask_(mod[0])
        contenitore = AppKit.NSMenuItem.alloc().init()
        contenitore.setTitle_(titolo)
        contenitore.setSubmenu_(menu)
        return contenitore, menu

    cmd, maiusc, ctrl = AppKit.NSCommandKeyMask, AppKit.NSShiftKeyMask, AppKit.NSControlKeyMask
    titoli = [barra.itemAtIndex_(i).title() for i in range(barra.numberOfItems())]
    if "Modifica" not in titoli:
        voce, _ = menu_nativo("Modifica", [
            ("Annulla", "undo:", "z"), ("Ripeti", "redo:", "z", cmd | maiusc), None,
            ("Taglia", "cut:", "x"), ("Copia", "copy:", "c"), ("Incolla", "paste:", "v"),
            ("Seleziona tutto", "selectAll:", "a")])
        barra.insertItem_atIndex_(voce, min(2, barra.numberOfItems()))
    if "Finestra" not in titoli:
        voce, menu = menu_nativo("Finestra", [
            ("Contrai", "performMiniaturize:", "m"), ("Ridimensiona", "performZoom:", ""), None,
            ("Attiva modalità a tutto schermo", "toggleFullScreen:", "f", cmd | ctrl), None,
            ("Porta tutto in primo piano", "arrangeInFront:", "")])
        indice_aiuto = next((i for i in range(barra.numberOfItems()) if barra.itemAtIndex_(i).title() == "Aiuto"),
                            barra.numberOfItems())
        barra.insertItem_atIndex_(voce, indice_aiuto)
        app.setWindowsMenu_(menu)
    for i in range(barra.numberOfItems()):
        sotto = barra.itemAtIndex_(i).submenu()
        if sotto is None:
            continue
        if barra.itemAtIndex_(i).title() == "Aiuto":
            app.setHelpMenu_(sotto)
        visti = set()
        for j in range(sotto.numberOfItems()):
            item = sotto.itemAtIndex_(j)
            titolo = item.title()
            # "Analisi" è sia un menu sia una voce di Vista: la scorciatoia va solo alla voce.
            if titolo in SCORCIATOIE and titolo not in visti and barra.itemAtIndex_(i).title() != titolo:
                tasto, con_maiusc = SCORCIATOIE[titolo]
                item.setKeyEquivalent_(tasto)
                item.setKeyEquivalentModifierMask_(cmd | (maiusc if con_maiusc else 0))
                visti.add(titolo)


def main():
    global FINESTRA
    import webview
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORTA), Gestore)
    except OSError:
        # Murnau è già aperto: si porta in primo piano la sua finestra.
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of '
                        '(first process whose name is "Murnau") to true'], capture_output=True)
        return
    # Prima che qualcosa possa scrivere: le cartelle di lavoro vanno dentro la cartella di Murnau, e
    # i file lasciati dalle versioni precedenti nelle cartelle dei risultati vanno nelle cartelle di lavoro.
    trasloca_tutte()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    identita_mac()
    FINESTRA = webview.create_window(
        "Murnau", f"http://127.0.0.1:{PORTA}", width=1240, height=900, min_size=(760, 560),
        background_color="#16161a", confirm_close=True, text_select=True)
    # Chiudendo la finestra si ferma un'eventuale analisi: si potrà riprendere.
    FINESTRA.events.closed += LAVORO.ferma

    def all_avvio():
        identita_mac()
        collega_trascinamento(FINESTRA)
        threading.Thread(target=segui_nel_dock, daemon=True).start()
        from PyObjCTools import AppHelper
        AppHelper.callAfter(sistema_menu)

    webview.settings["SHOW_DEFAULT_MENUS"] = False
    webview.start(all_avvio, menu=crea_menu(), localization={
        "cocoa.menu.about": "Informazioni su", "cocoa.menu.services": "Servizi", "cocoa.menu.hide": "Nascondi",
        "cocoa.menu.hideOthers": "Nascondi altre", "cocoa.menu.showAll": "Mostra tutte", "cocoa.menu.quit": "Esci da",
        "global.quitConfirmation": "Chiudere Murnau? Un'analisi in corso verrà fermata: rilanciandola "
                                   "riprenderà dal punto in cui si è interrotta.",
        "global.ok": "Chiudi", "global.cancel": "Annulla", "global.quit": "Esci da Murnau",
    })
    LAVORO.ferma()
    server.shutdown()


if __name__ == "__main__":
    main()
