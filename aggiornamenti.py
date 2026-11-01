"""Controllo e installazione degli aggiornamenti di Murnau e dei suoi componenti.

Si controllano:
- Murnau stesso: un file di annuncio (JSON) pubblicato online, il cui indirizzo è
  in aggiornamenti.json accanto all'app, dice qual è l'ultima versione e dove
  scaricarla. Il pacchetto è uno zip con i file dell'app, verificato con SHA-256;
  i file sostituiti vengono conservati per poter tornare indietro. Senza indirizzo
  questa parte si salta;
- i programmi installati con Homebrew: ffmpeg (fotogrammi) e Ollama (che fa girare
  Qwen3-VL);
- le librerie Python dell'ambiente (pywebview, OpenCV, PyTorch, Transformers…):
  l'ambiente è condiviso con Vertov, quindi dopo l'aggiornamento si verifica che si
  carichino le librerie di entrambe le app, e se qualcosa non va si torna alle
  versioni di prima;
- i modelli: Qwen3-VL (registro di Ollama) e OWLv2 (Hugging Face).

Annuncio (murnau.json):
    {"versione": "1.1.0", "data": "2026-11-20", "novita": ["…", "…"],
     "pacchetto": "murnau-1.1.0.zip", "sha256": "…"}
"pacchetto" può essere un indirizzo completo o relativo all'annuncio.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import rilevatori

CARTELLA = Path(__file__).resolve().parent
DATI = Path.home() / "Library" / "Application Support" / "Murnau"
STATO = DATI / "aggiornamenti.json"
PRECEDENTI = DATI / "versioni_precedenti"
OLLAMA = "http://localhost:11434"
REGISTRO = "https://registry.ollama.ai/v2"
HF_CACHE = rilevatori.CACHE_HF
OGNI = 24 * 3600  # controllo automatico al massimo una volta al giorno
# Cartelle dell'app che un aggiornamento non tocca mai.
INTOCCABILI = {"prova_risultati", "dist", "__pycache__"}

PROGRAMMI = {"ffmpeg": "estrae i fotogrammi dal film",
             "ollama": "fa girare Qwen3-VL sul Mac"}
MODELLI_HF = {rilevatori.MODELLO_OGGETTI: "OWLv2, cerca microfoni, macchine da presa, stativi, prese e cavi"}
# Moduli che devono caricarsi dopo un aggiornamento delle librerie: quelli di Murnau e,
# dato che l'ambiente è lo stesso, anche quelli di Vertov.
MODULI = ["webview", "aaf2", "cv2", "numpy", "PIL", "torch", "torchvision", "transformers", "huggingface_hub",
          "scipy", "pypdf", "mlx_whisper"]
GRUPPI = {"app": "Murnau", "programma": "Programmi", "libreria": "Librerie Python",
          "modello": "Modelli di intelligenza artificiale"}


def versione() -> str:
    try:
        return (CARTELLA / "VERSIONE").read_text().strip()
    except OSError:
        return "0"


def _numeri(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def indirizzo_annuncio() -> str:
    try:
        return json.loads((CARTELLA / "aggiornamenti.json").read_text()).get("indirizzo", "").strip()
    except (OSError, ValueError):
        return ""


def _sicuro(indirizzo: str) -> bool:
    u = urlparse(indirizzo)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))


def leggi_stato() -> dict:
    try:
        return {"automatico": True, "saltati": {}, **json.loads(STATO.read_text())}
    except (OSError, ValueError):
        return {"automatico": True, "saltati": {}}


def scrivi_stato(**valori) -> dict:
    stato = {**leggi_stato(), **valori}
    DATI.mkdir(parents=True, exist_ok=True)
    STATO.write_text(json.dumps(stato, ensure_ascii=False, indent=2))
    return stato


def _scarica_json(indirizzo: str, intestazioni: dict = None):
    richiesta = urllib.request.Request(indirizzo, headers={"User-Agent": f"Murnau/{versione()}", **(intestazioni or {})})
    with urllib.request.urlopen(richiesta, timeout=15) as r:
        corpo = r.read()
        return json.loads(corpo), corpo


def brew() -> str:
    return shutil.which("brew") or "/opt/homebrew/bin/brew"


def _messaggio(e: Exception) -> str:
    if isinstance(e, urllib.error.URLError):
        return "non riesco a collegarmi (sei online?)"
    if isinstance(e, subprocess.TimeoutExpired):
        return "non ha risposto in tempo"
    return str(e)


# --------------------------------------------------------------------------
# Controllo: ogni aggiornamento trovato è una "voce"
#   {chiave, gruppo, nome, descrizione, attuale, nuova, impronta, …}
# "impronta" identifica la versione nuova, per poterla saltare.
# --------------------------------------------------------------------------

def controlla_app() -> tuple:
    """(configurato, voce o None)."""
    indirizzo = indirizzo_annuncio()
    if not indirizzo:
        return False, None
    if not _sicuro(indirizzo):
        raise RuntimeError("l'indirizzo degli aggiornamenti deve essere https")
    annuncio, _ = _scarica_json(indirizzo)
    nuova = str(annuncio.get("versione", ""))
    if not nuova or _numeri(nuova) <= _numeri(versione()):
        return True, None
    return True, {"chiave": "app", "gruppo": "app", "nome": "Murnau", "descrizione": "l'app",
                  "attuale": versione(), "nuova": nuova, "impronta": nuova, "data": annuncio.get("data", ""),
                  "novita": annuncio.get("novita") or [],
                  "pacchetto": urljoin(indirizzo, annuncio["pacchetto"]), "sha256": annuncio["sha256"].lower()}


def controlla_programmi() -> list:
    subprocess.run([brew(), "update", "--quiet"], capture_output=True, timeout=180)
    r = subprocess.run([brew(), "outdated", "--json=v2", *PROGRAMMI], capture_output=True, text=True, timeout=60)
    voci = []
    for f in json.loads(r.stdout or "{}").get("formulae", []):
        voci.append({"chiave": "programma:" + f["name"], "gruppo": "programma", "nome": f["name"],
                     "descrizione": PROGRAMMI.get(f["name"], ""), "attuale": f["installed_versions"][-1],
                     "nuova": f["current_version"], "impronta": f["current_version"]})
    return voci


def controlla_librerie() -> list:
    # Solo le librerie installate direttamente (non quelle che si portano dietro): le altre seguono da sé.
    r = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--not-required", "--format=json",
                        "--disable-pip-version-check"], capture_output=True, text=True, timeout=180)
    return [{"chiave": "libreria:" + p["name"], "gruppo": "libreria", "nome": p["name"],
             "descrizione": "condivisa con Vertov", "attuale": p["version"], "nuova": p["latest_version"],
             "impronta": p["latest_version"]}
            for p in json.loads(r.stdout or "[]") if p["name"] != "pip"]


def controlla_modello_visione(modello: str) -> list:
    """Confronta l'impronta del modello scaricato con quella del registro di Ollama."""
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
        locali = {m["name"]: m.get("digest", "") for m in json.load(r).get("models", [])}
    if modello not in locali:
        return []
    nome, _, etichetta = modello.partition(":")
    percorso = nome if "/" in nome else f"library/{nome}"
    _, corpo = _scarica_json(f"{REGISTRO}/{percorso}/manifests/{etichetta or 'latest'}",
                             {"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    remota = hashlib.sha256(corpo).hexdigest()
    if remota == locali[modello]:
        return []
    return [{"chiave": "ollama:" + modello, "gruppo": "modello", "nome": modello,
             "descrizione": "Qwen3-VL, esamina i fotogrammi e verifica i sospetti (download di circa 20 GB; "
                            "lo usa anche Vertov)",
             "attuale": locali[modello][:12], "nuova": remota[:12], "impronta": remota}]


def controlla_modelli_hf() -> list:
    voci = []
    for repo, descrizione in MODELLI_HF.items():
        ref = HF_CACHE / ("models--" + repo.replace("/", "--")) / "refs" / "main"
        if not ref.exists():
            continue  # non ancora scaricato: lo scarica Murnau la prima volta che serve
        locale = ref.read_text().strip()
        remoto, _ = _scarica_json(f"https://huggingface.co/api/models/{repo}")
        if remoto.get("sha") and remoto["sha"] != locale:
            voci.append({"chiave": "hf:" + repo, "gruppo": "modello", "nome": repo.split("/")[-1],
                         "descrizione": descrizione, "attuale": locale[:12], "nuova": remoto["sha"][:12],
                         "impronta": remoto["sha"], "repo": repo})
    return voci


def controlla(modello: str, manuale: bool = False) -> dict:
    """Cerca aggiornamenti di tutto. Se automatico, non ripropone quelli saltati."""
    stato = leggi_stato()
    esito = {"versione": versione(), "voci": [], "errori": [], "configurato": True,
             "automatico": stato.get("automatico", True)}
    try:
        esito["configurato"], voce = controlla_app()
        esito["voci"] += [voce] if voce else []
    except Exception as e:
        esito["errori"].append(f"Murnau: {_messaggio(e)}")
    for nome, funzione in [("programmi", controlla_programmi), ("librerie", controlla_librerie),
                           ("Qwen3-VL", lambda: controlla_modello_visione(modello)),
                           ("OWLv2", controlla_modelli_hf)]:
        try:
            esito["voci"] += funzione()
        except Exception as e:
            esito["errori"].append(f"{nome}: {_messaggio(e)}")
    scrivi_stato(ultimo_controllo=time.time())
    if not manuale:
        saltati = stato.get("saltati", {})
        esito["voci"] = [v for v in esito["voci"] if saltati.get(v["chiave"]) != v["impronta"]]
    return esito


def salta(voci: list) -> None:
    saltati = leggi_stato().get("saltati", {})
    saltati.update({v["chiave"]: v["impronta"] for v in voci})
    scrivi_stato(saltati=saltati)


def da_controllare() -> bool:
    stato = leggi_stato()
    return stato.get("automatico", True) and time.time() - stato.get("ultimo_controllo", 0) > OGNI


# --------------------------------------------------------------------------
# Installazione (in un thread; l'interfaccia legge INSTALLAZIONE)
# --------------------------------------------------------------------------

INSTALLAZIONE = {"attiva": False, "fase": "", "fatti": 0, "totale": 0, "errore": "", "riavvia": False,
                 "fatto": False, "riusciti": [], "falliti": []}


def _fase(fase: str, fatti: int = 0, totale: int = 0) -> None:
    INSTALLAZIONE.update(fase=fase, fatti=fatti, totale=totale)


def _installa_app(voce: dict) -> None:
    _fase("Scarico Murnau " + voce["nuova"] + " (KB)")
    if not _sicuro(voce["pacchetto"]):
        raise RuntimeError("Il pacchetto deve essere scaricato via https.")
    with tempfile.TemporaryDirectory() as tmp:
        zip_ = Path(tmp) / "murnau.zip"
        impronta = hashlib.sha256()
        richiesta = urllib.request.Request(voce["pacchetto"], headers={"User-Agent": f"Murnau/{versione()}"})
        with urllib.request.urlopen(richiesta, timeout=60) as r, open(zip_, "wb") as f:
            totale = int(r.headers.get("Content-Length") or 0)
            while blocco := r.read(1 << 16):
                f.write(blocco)
                impronta.update(blocco)
                _fase(INSTALLAZIONE["fase"], f.tell() // 1024, totale // 1024)
        if impronta.hexdigest() != voce["sha256"]:
            raise RuntimeError("Il pacchetto scaricato non corrisponde all'impronta annunciata: non lo installo.")

        _fase("Controllo il pacchetto")
        estratti = Path(tmp) / "nuova"
        with zipfile.ZipFile(zip_) as z:
            for nome in z.namelist():
                destinazione = (estratti / nome).resolve()
                if estratti.resolve() not in destinazione.parents and destinazione != estratti.resolve():
                    raise RuntimeError("Il pacchetto contiene percorsi non validi.")
            z.extractall(estratti)
            # extractall non conserva i permessi: senza, il lanciatore di Murnau.app non sarebbe più eseguibile.
            for info in z.infolist():
                permessi = info.external_attr >> 16 & 0o777
                if permessi and not info.is_dir():
                    (estratti / info.filename).chmod(permessi)
        radice = estratti
        voci = [p for p in estratti.iterdir() if p.name != "__MACOSX"]
        if len(voci) == 1 and voci[0].is_dir():
            radice = voci[0]
        if not (radice / "app.py").exists() or not (radice / "murnau.py").exists():
            raise RuntimeError("Il pacchetto non contiene Murnau.")

        _fase("Installo Murnau " + voce["nuova"])
        copia = PRECEDENTI / versione()
        for sorgente in sorted(radice.rglob("*")):
            rel = sorgente.relative_to(radice)
            if rel.parts[0] in INTOCCABILI or sorgente.name == ".DS_Store" or sorgente.is_dir():
                continue
            bersaglio = CARTELLA / rel
            if bersaglio.exists():
                (copia / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bersaglio, copia / rel)
            bersaglio.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sorgente, bersaglio)
    INSTALLAZIONE["riavvia"] = True


def _installa_programmi(nomi: list) -> None:
    _fase("Aggiorno " + ", ".join(nomi) + " con Homebrew")
    r = subprocess.run([brew(), "upgrade", *nomi], capture_output=True, text=True, timeout=3600)
    if r.returncode:
        raise RuntimeError("Homebrew: " + (r.stderr.strip().splitlines() or ["errore"])[-1])
    if "ollama" in nomi:
        # Ollama gira in sottofondo: va riavviato perché usi la versione nuova.
        _fase("Riavvio Ollama")
        servizi = subprocess.run([brew(), "services", "list"], capture_output=True, text=True).stdout
        if any(r.split()[:1] == ["ollama"] for r in servizi.splitlines()):
            subprocess.run([brew(), "services", "restart", "ollama"], capture_output=True)
        for _ in range(30):
            time.sleep(1)
            try:
                urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=2)
                break
            except Exception:
                pass


def _installa_librerie(nomi: list) -> None:
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    prima = subprocess.run([*pip, "freeze"], capture_output=True, text=True).stdout
    _fase("Aggiorno le librerie Python")
    r = subprocess.run([*pip, "install", "-q", "-U", *nomi], capture_output=True, text=True, timeout=3600)
    prova = subprocess.run([sys.executable, "-c", "import " + ", ".join(MODULI)], capture_output=True, text=True,
                           timeout=600)
    if r.returncode or prova.returncode:
        _fase("Qualcosa non va: rimetto le librerie di prima")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(prima)
        subprocess.run([*pip, "install", "-q", "-r", f.name], capture_output=True, timeout=3600)
        Path(f.name).unlink(missing_ok=True)
        dettaglio = (r.stderr or prova.stderr).strip().splitlines()
        raise RuntimeError("le librerie nuove non funzionano con Murnau o con Vertov, ho rimesso quelle di prima"
                           + (f" ({dettaglio[-1]})" if dettaglio else ""))


def _installa_modello_visione(modello: str) -> None:
    _fase("Aggiorno Qwen3-VL (MB)")
    corpo = json.dumps({"model": modello, "stream": True}).encode()
    richiesta = urllib.request.Request(f"{OLLAMA}/api/pull", data=corpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(richiesta, timeout=None) as r:
        for riga in r:
            stato = json.loads(riga or b"{}")
            if stato.get("error"):
                raise RuntimeError(f"download non riuscito: {stato['error']}")
            if stato.get("total") and stato.get("completed") is not None:
                _fase(INSTALLAZIONE["fase"], stato["completed"] // 2**20, stato["total"] // 2**20)


def _installa_modello_hf(repo: str) -> None:
    _fase(f"Aggiorno {repo.split('/')[-1]}")
    r = subprocess.run([sys.executable, "-c", "import sys; from huggingface_hub import snapshot_download; "
                        "snapshot_download(sys.argv[1], cache_dir=sys.argv[2])", repo, str(HF_CACHE)],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode:
        raise RuntimeError("download non riuscito: " + (r.stderr.strip().splitlines() or ["errore"])[-1])


def installa(voci: list, modello: str) -> None:
    if INSTALLAZIONE["attiva"]:
        raise RuntimeError("Un aggiornamento è già in corso.")
    INSTALLAZIONE.update(attiva=True, errore="", riavvia=False, fatto=False, fase="", fatti=0, totale=0,
                         riusciti=[], falliti=[])
    per_gruppo = {g: [v for v in voci if v["gruppo"] == g] for g in GRUPPI}

    # Prima i componenti, per ultima l'app (che chiede di riavviare). Un passo fallito non ferma gli altri.
    passi = []
    if per_gruppo["libreria"]:
        passi.append(("Librerie Python", lambda: _installa_librerie([v["nome"] for v in per_gruppo["libreria"]])))
    if per_gruppo["programma"]:
        passi.append((", ".join(v["nome"] for v in per_gruppo["programma"]),
                      lambda: _installa_programmi([v["nome"] for v in per_gruppo["programma"]])))
    for v in per_gruppo["modello"]:
        if v["chiave"].startswith("ollama:"):
            passi.append((v["nome"], lambda: _installa_modello_visione(modello)))
        else:
            passi.append((v["nome"], lambda repo=v["repo"]: _installa_modello_hf(repo)))
    for v in per_gruppo["app"]:
        passi.append((f"Murnau {v['nuova']}", lambda v=v: _installa_app(v)))

    def lavoro():
        try:
            for nome, passo in passi:
                try:
                    passo()
                    INSTALLAZIONE["riusciti"].append(nome)
                except Exception as e:
                    INSTALLAZIONE["falliti"].append(f"{nome}: {_messaggio(e)}")
            fatti = {v["chiave"] for v in voci}
            scrivi_stato(saltati={k: i for k, i in leggi_stato().get("saltati", {}).items() if k not in fatti})
            INSTALLAZIONE["errore"] = "; ".join(INSTALLAZIONE["falliti"])
            INSTALLAZIONE["fatto"] = True
        finally:
            INSTALLAZIONE["attiva"] = False

    threading.Thread(target=lavoro, daemon=True).start()


def riavvia(porta: int) -> None:
    """Riapre Murnau appena questa istanza ha liberato la porta del server."""
    bundle = CARTELLA / "Murnau.app"
    apri = ["open", str(bundle)] if bundle.exists() else [sys.executable, str(CARTELLA / "app.py")]
    script = (f"for i in $(seq 1 60); do nc -z 127.0.0.1 {porta} || break; sleep 0.5; done; "
              + " ".join(f"'{a}'" for a in apri))
    subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
