"""Libera spazio: file temporanei e di cache creati da Murnau.

Ogni voce dice quanto occupa e se si può cancellare senza pensarci (si ricrea da
sola quando serve) o se conviene lasciarla. Non tocca mai i materiali, le
segnalazioni, le revisioni né le sessioni: solo cose che Murnau ha creato o scaricato.
"""

import json
import re
import shutil
import urllib.request
from pathlib import Path

import rilevatori

DATI = Path.home() / "Library" / "Application Support" / "Murnau"
PIP_CACHE = Path.home() / "Library" / "Caches" / "pip"
OLLAMA = "http://localhost:11434"
# Modelli Hugging Face che Murnau usa davvero: gli altri nella sua cartella sono resti di prove.
MODELLI_HF_USATI = {rilevatori.MODELLO_OGGETTI}


def _dimensione(percorso: Path) -> int:
    if percorso.is_file():
        return percorso.stat().st_size
    totale = 0
    for f in percorso.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                totale += f.stat().st_size
        except OSError:
            pass
    return totale


def _cartelle_fotogrammi() -> list:
    """I fotogrammi estratti, nella cartella di lavoro di ogni sessione."""
    cartelle = set()
    for f in (DATI / "sessioni").glob("*.json"):
        try:
            lavoro = json.loads(f.read_text()).get("cartella_lavoro")
        except (OSError, ValueError):
            continue
        if lavoro and (Path(lavoro) / "lavoro" / "fotogrammi").is_dir():
            cartelle.add(Path(lavoro) / "lavoro" / "fotogrammi")
    return sorted(cartelle)


def _modelli_hf_non_usati() -> list:
    cartelle = {c.name[len("models--"):].replace("--", "/", 1): c for c in rilevatori.CACHE_HF.glob("models--*")}
    return [(repo, c, _dimensione(c)) for repo, c in cartelle.items() if repo not in MODELLI_HF_USATI]


# Le app sorelle (Vertov, Ozu...): il loro codice sorgente sta accanto a quello di Murnau in My Drive/CLAUDE,
# e le app installate hanno il programma dentro, in Contents/Resources/programma.
CODICE_FAMIGLIA = Path.home() / "My Drive" / "CLAUDE"
APP_INSTALLATE = Path.home() / "Applications"


def _programmi_della_famiglia():
    yield from CODICE_FAMIGLIA.glob("*/*.py")
    yield from APP_INSTALLATE.glob("*.app/Contents/Resources/programma/*.py")


def modelli_protetti() -> set:
    """I modelli di Ollama usati da Murnau e dalle app sorelle: Libera spazio non li propone mai.

    Quelli delle altre app si leggono dal loro codice (righe come MODELLO = "gemma4:26b" o REVISORE = "qwen3.8:27b-dflash2-nvfp4"), così una
    scelta nuova di Vertov o di Ozu è protetta senza dover cambiare Murnau."""
    protetti = set(rilevatori.MODELLI)
    for f in _programmi_della_famiglia():
        try:
            # Ogni costante che nomina un modello (MODELLO, REVISORE di Ozu...), non gli indirizzi.
            protetti |= {n for n in re.findall(r'^\s*[A-Z][A-Z0-9_]* *= *"([\w.:/-]+:[\w.:-]+)"',
                                               f.read_text(errors="ignore"), re.M) if "//" not in n}
        except OSError:
            pass
    return protetti


def _modelli_ollama_non_usati() -> list:
    protetti = modelli_protetti()
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            return [(m["name"], m.get("size", 0)) for m in json.load(r).get("models", []) if m["name"] not in protetti]
    except Exception:
        return []


def voci() -> list:
    """Cosa si può cancellare, con la dimensione in byte."""
    v = []
    fotogrammi = _cartelle_fotogrammi()
    v.append({"chiave": "fotogrammi", "nome": "Fotogrammi estratti dai film",
              "spiega": "Le immagini usate durante l'analisi. Le segnalazioni restano; se rilanci un'analisi "
                        "Murnau le ricrea.",
              "byte": sum(_dimensione(p) for p in fotogrammi), "consigliata": True})
    v.append({"chiave": "versioni", "nome": "Versioni precedenti di Murnau",
              "spiega": "Copie dei file sostituiti dagli aggiornamenti, per poter tornare indietro.",
              "byte": _dimensione(DATI / "versioni_precedenti") if (DATI / "versioni_precedenti").exists() else 0,
              "consigliata": True})
    registro = DATI / "murnau.log"
    v.append({"chiave": "registri", "nome": "Registro",
              "spiega": "Il diario tecnico delle analisi, utile solo per capire un errore.",
              "byte": registro.stat().st_size if registro.exists() else 0, "consigliata": True})
    v.append({"chiave": "pip", "nome": "File scaricati per installare le librerie",
              "spiega": "Copie dei pacchetti Python scaricati durante le installazioni. Non servono più.",
              "byte": _dimensione(PIP_CACHE) if PIP_CACHE.exists() else 0, "consigliata": True})
    hf = _modelli_hf_non_usati()
    v.append({"chiave": "modelli_hf", "nome": "Modelli di prova non usati",
              "spiega": "Modelli nella cartella di Murnau che Murnau non usa: " +
                        (", ".join(r for r, *_ in hf) or "nessuno") + ".",
              "byte": sum(b for *_, b in hf), "consigliata": True})
    ollama = _modelli_ollama_non_usati()
    v.append({"chiave": "modelli_ollama", "nome": "Altri modelli in Ollama",
              "spiega": "Modelli di Ollama che né Murnau né Vertov né Ozu usano: " +
                        (", ".join(n for n, _ in ollama) or "nessuno") +
                        ". Lasciali se usi Ollama anche per altro.",
              "byte": sum(b for _, b in ollama), "consigliata": False})
    return [x for x in v if x["byte"] > 0]


def cancella(chiavi: list) -> int:
    """Cancella le voci scelte e restituisce i byte liberati."""
    liberati = 0
    if "fotogrammi" in chiavi:
        for p in _cartelle_fotogrammi():
            liberati += _dimensione(p)
            shutil.rmtree(p, ignore_errors=True)
    if "versioni" in chiavi and (DATI / "versioni_precedenti").exists():
        liberati += _dimensione(DATI / "versioni_precedenti")
        shutil.rmtree(DATI / "versioni_precedenti", ignore_errors=True)
    if "registri" in chiavi and (DATI / "murnau.log").exists():
        liberati += (DATI / "murnau.log").stat().st_size
        (DATI / "murnau.log").open("w").close()  # l'app ci sta scrivendo: si svuota, non si cancella
    if "pip" in chiavi and PIP_CACHE.exists():
        liberati += _dimensione(PIP_CACHE)
        shutil.rmtree(PIP_CACHE, ignore_errors=True)
    if "modelli_hf" in chiavi:
        for _, cartella, byte in _modelli_hf_non_usati():
            shutil.rmtree(cartella, ignore_errors=True)
            liberati += byte
    if "modelli_ollama" in chiavi:
        for nome, byte in _modelli_ollama_non_usati():
            richiesta = urllib.request.Request(f"{OLLAMA}/api/delete", method="DELETE",
                                               data=json.dumps({"model": nome}).encode(),
                                               headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(richiesta, timeout=30)
                liberati += byte
            except Exception:
                pass
    return liberati
