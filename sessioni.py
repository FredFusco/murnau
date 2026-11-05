"""Le sessioni di Murnau e i loro materiali.

Una sessione è un progetto di lavoro: il film, il montaggio, la sua cartella di lavoro,
la cartella in cui esportare, le opzioni e un nome. Ogni modifica viene salvata subito,
in DATI/sessioni/<id>.json, con una scrittura che non lascia mai un file a metà.

I file con cui Murnau lavora (fotogrammi, rilevamenti, segnalazioni, revisione...) stanno
in una cartella di lavoro per sessione, dentro la cartella di Murnau; nella cartella di
destinazione scelta dall'utente finisce solo ciò che esporta.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import montaggio
import rilevatori
from archivi import scrivi_json

CARTELLA = Path(__file__).resolve().parent
DATI = Path.home() / "Library" / "Application Support" / "Murnau"
SESSIONI = DATI / "sessioni"
FILE_CORRENTE = DATI / "sessione_corrente.json"
FILE_VECCHIO = DATI / "progetto.json"  # prima delle sessioni
ESTENSIONI_VIDEO = {".mov", ".mp4", ".mxf", ".m4v", ".avi", ".mkv"}
ESTENSIONI_MONTAGGIO = {".edl", ".aaf"}
RUOLI = {"video": "Film", "montaggio": "Montaggio"}
# I file con cui Murnau lavora (fotogrammi, rilevamenti, segnalazioni, revisione...) stanno qui, con gli altri
# dati di Murnau, una cartella per sessione. Nella cartella di destinazione scelta dall'utente finisce solo
# ciò che esporta. Non stanno nella cartella del programma: dentro Murnau.app non si scrive mai (la firma
# dell'app resterebbe rotta), e l'app funziona anche su un Mac senza la cartella del codice.
FILE_DI_LAVORO = DATI / "file di lavoro"
# Dove le versioni fino alla 1.0.9 tenevano le cartelle di lavoro: accanto al programma, nel codice sorgente.
VECCHIO_NOME_FILE_DI_LAVORO = "Murnau – file di lavoro"


# --------------------------------------------------------------------------
# Sessioni
# --------------------------------------------------------------------------

def _file_sessione(id_: str) -> Path:
    return SESSIONI / f"{id_}.json"


def _registra(p: dict) -> None:
    """Salva la sessione senza cambiare quella aperta."""
    SESSIONI.mkdir(parents=True, exist_ok=True)
    scrivi_json(_file_sessione(p["id"]), p)


def _scrivi_sessione(p: dict) -> None:
    """Salva la sessione e la rende quella aperta."""
    p["modificata"] = time.strftime("%Y-%m-%d %H:%M")
    _registra(p)
    scrivi_json(FILE_CORRENTE, {"id": p["id"]}, indent=None)


def leggi_sessione(id_: str) -> dict:
    return json.loads(_file_sessione(id_).read_text())


def nuova_sessione(nome: str = "", dati: dict = None) -> dict:
    id_ = time.strftime("%Y%m%d-%H%M%S")
    while _file_sessione(id_).exists():
        id_ += "b"
    p = {"categorie": list(rilevatori.CATEGORIE), "accurato": False, **(dati or {}),
         "id": id_, "nome": nome.strip() or "Nuova sessione", "creata": time.strftime("%Y-%m-%d %H:%M")}
    _scrivi_sessione(p)
    return p


def carica_progetto() -> dict:
    """La sessione aperta. Se non si riesce a leggerla si riprova per qualche istante (un altro
    programma, per esempio la sincronizzazione di Drive, potrebbe averla in mano), poi si apre la
    più recente. Se ne crea una nuova solo se non ce n'è nessuna leggibile."""
    for tentativo in range(5):
        try:
            id_ = json.loads(FILE_CORRENTE.read_text())["id"]
        except (OSError, ValueError, KeyError, TypeError):
            id_ = None
        if id_ is None or not _file_sessione(id_).exists():
            break
        try:
            return leggi_sessione(id_)
        except (OSError, ValueError):
            time.sleep(0.1 * (tentativo + 1))
    for voce in elenco_sessioni():
        try:
            p = leggi_sessione(voce["id"])
        except (OSError, ValueError):
            continue
        _scrivi_sessione(p)
        return p
    if any(SESSIONI.glob("*.json")):
        raise RuntimeError("Non riesco a leggere le sessioni di Murnau: il disco potrebbe avere un problema. "
                           "Riprova tra poco.")
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
        if _file_sessione(p["id"]).exists():
            _registra(p)
    c = _lavoro(p)
    c.mkdir(parents=True, exist_ok=True)
    return c


def rinomina_cartella_lavoro(p: dict, in_uso: bool = False) -> None:
    """Dopo aver cambiato nome alla sessione, anche la sua cartella di lavoro prende il nuovo nome.
    Non se un'analisi ci sta scrivendo (`in_uso`): si rinominerà alla prossima occasione."""
    vecchia = _lavoro(p)
    nuova = FILE_DI_LAVORO / nome_per_file(p.get("nome"))
    if in_uso or not vecchia.is_dir() or nuova == vecchia or nuova.exists() or vecchia.parent != FILE_DI_LAVORO:
        return
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
        if vecchia.parent.name == VECCHIO_NOME_FILE_DI_LAVORO and not rimasti:
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
                _registra(p)
                print(f"Sessione {p.get('nome')}: {esito}")
        except Exception as e:
            print(f"Sessione {f.stem}: cartella di lavoro non spostata ({e})")
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
            spostati = trasloca(p)
            if spostati:
                _registra(p)
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
    p = leggi_sessione(id_)
    altre = []
    for f in SESSIONI.glob("*.json"):
        if f.stem != id_:
            try:
                altre.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                pass
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
        prossima = leggi_sessione(rimaste[0]["id"]) if rimaste else nuova_sessione()
        _scrivi_sessione(prossima)
    return {"spostati": spostati}


def numero_segnalazioni(p: dict):
    """Quante segnalazioni ha l'ultima analisi della sessione (None se non ce n'è una)."""
    try:
        return len(json.loads((_lavoro(p) / "segnalazioni.json").read_text())["segnalazioni"])
    except (OSError, ValueError, KeyError):
        return None


def elenco_sessioni() -> list:
    elenco = []
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        segnalazioni = numero_segnalazioni(p)
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

