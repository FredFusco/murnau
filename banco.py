#!/usr/bin/env python3
"""Il banco di prova: le segnalazioni che l'utente ha rivisto, conservate per misurare i modelli.

Ogni segnalazione segnata come «errore confermato» o «falso allarme» diventa un caso: le sue
immagini (il fotogramma con il riquadro, il dettaglio e, se si ritrova, il fotogramma pulito) e il
giudizio dell'utente. I casi stanno in ~/Library/Application Support/Murnau/banco, sul Mac e mai su
GitHub, e restano anche se la sessione o i suoi file di lavoro vengono cancellati.

Serve a due cose:
- le statistiche: per categoria, e per le segnalazioni «da controllare» quante volte aveva ragione
  il modello della verifica (Gemma 4) e quante la seconda opinione (Qwen3-VL);
- la prova dei modelli: rifà verifica, esame e misura dello sguardo su tutti i casi e confronta
  con i giudizi dell'utente. Da usare prima di cambiare un modello o le domande.

    ~/.venvs/descrittore/bin/python banco.py              # statistiche
    ~/.venvs/descrittore/bin/python banco.py prova        # prova i modelli attuali
    ~/.venvs/descrittore/bin/python banco.py prova --verifica gemma4:31b
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import sessioni
from archivi import scrivi_json

BANCO = sessioni.DATI / "banco"
CASI = BANCO / "casi"
PROVE = BANCO / "prove"


# --------------------------------------------------------------------------
# Raccolta dei casi
# --------------------------------------------------------------------------

def _chiave(cartella: Path, s: dict) -> str:
    """Stabile anche se la sessione o la sua cartella cambiano nome: dipende dall'immagine."""
    try:
        impronta = hashlib.sha1((cartella / s["immagine"]).read_bytes()).hexdigest()[:16]
    except OSError:
        impronta = hashlib.sha1(f"{cartella}|{s['id']}".encode()).hexdigest()[:16]
    return f"{impronta}-{s['id']}"


def _fotogramma_pulito(cartella: Path, s: dict):
    """Il fotogramma senza riquadro da cui viene la segnalazione, cercato negli archivi dell'analisi."""
    lavoro = cartella / "lavoro"
    for archivio in sorted(lavoro.glob("*.jsonl")):
        try:
            righe = archivio.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for riga in righe:
            try:
                r = json.loads(riga)
            except ValueError:
                continue
            voci = r.get("v") if isinstance(r.get("v"), list) else []
            if any(isinstance(v, dict) and v.get("riquadro") == s.get("riquadro") for v in voci):
                f = lavoro / "fotogrammi" / r["k"].split("|")[0]
                if f.exists():
                    return f
    return None


def registra(cartella: Path, ident: str = None) -> int:
    """Aggiorna i casi di una cartella di lavoro (o solo quello di `ident`). Restituisce quanti ne ha toccati."""
    cartella = Path(cartella)
    import esporta
    try:
        dati, revisione = esporta.leggi(cartella)  # solo i giudizi che riguardano queste segnalazioni
    except (OSError, ValueError):
        return 0
    modelli = dati.get("modelli") or {}
    if not modelli and any("da_controllare" in s for s in dati.get("segnalazioni", [])):
        # Analisi a due livelli fatta prima che Murnau annotasse i modelli (1.0.5): erano questi.
        modelli = {"esame": "qwen3-vl:30b-a3b-instruct", "verifica": "gemma4:26b",
                   "seconda_opinione": "qwen3-vl:30b-a3b-instruct", "dedotti": True}
    toccati = 0
    for s in dati.get("segnalazioni", []):
        if ident and s["id"] != ident:
            continue
        caso = CASI / _chiave(cartella, s)
        giudizio = revisione.get(s["id"])
        if giudizio not in ("confermato", "falso"):
            if caso.exists():  # la revisione è stata tolta: il caso non è più valutato
                shutil.rmtree(caso, ignore_errors=True)
                toccati += 1
            continue
        caso.mkdir(parents=True, exist_ok=True)
        for nome, relativo in (("segnato.jpg", s.get("immagine")), ("dettaglio.jpg", s.get("dettaglio"))):
            if relativo and not (caso / nome).exists() and (cartella / relativo).exists():
                shutil.copyfile(cartella / relativo, caso / nome)
        if not (caso / "fotogramma.jpg").exists():
            pulito = _fotogramma_pulito(cartella, s)
            if pulito:
                shutil.copyfile(pulito, caso / "fotogramma.jpg")
        scrivi_json(caso / "caso.json", {
            "id": s["id"], "categoria": s["categoria"], "vero": giudizio == "confermato",
            "nota": revisione.get("note:" + s["id"], ""), "riquadro": s.get("riquadro"),
            "origine": s.get("origine", ""), "oggetto": s.get("oggetto", ""), "descrizione": s.get("descrizione", ""),
            "certezza": s.get("certezza", ""), "da_controllare": bool(s.get("da_controllare")),
            "seconda_opinione": s.get("seconda_opinione", ""), "modelli": modelli,
            "analisi": dati.get("creato", ""), "rivisto": time.strftime("%Y-%m-%d %H:%M")})
        toccati += 1
    return toccati


def aggiorna_tutto() -> int:
    """Raccoglie le revisioni di tutte le sessioni (all'avvio dell'app)."""
    cartelle = set()
    for f in sessioni.SESSIONI.glob("*.json"):
        try:
            cartelle.add(sessioni._lavoro(json.loads(f.read_text())))
        except (OSError, ValueError):
            continue
    return sum(registra(c) for c in cartelle if (c / "revisione.json").exists())


def casi() -> list:
    elenco = []
    for f in sorted(CASI.glob("*/caso.json")):
        try:
            elenco.append({**json.loads(f.read_text()), "cartella": str(f.parent)})
        except (OSError, ValueError):
            continue
    return elenco


# --------------------------------------------------------------------------
# Statistiche
# --------------------------------------------------------------------------

def statistiche() -> dict:
    import rilevatori
    tutti = casi()
    per_categoria = {}
    for c in tutti:
        voce = per_categoria.setdefault(c["categoria"], {"titolo": rilevatori.CATEGORIE.get(c["categoria"], c["categoria"]),
                                                          "veri": 0, "falsi": 0})
        voce["veri" if c["vero"] else "falsi"] += 1
    # Solo le analisi fatte con i due livelli (dalla 1.0.5) dicono chi dei due modelli aveva ragione.
    due_livelli = [c for c in tutti if c.get("modelli", {}).get("seconda_opinione") and c["categoria"] != "sguardi"]
    dubbie = [c for c in due_livelli if c["da_controllare"]]
    concordi = [c for c in due_livelli if not c["da_controllare"]]
    try:
        ultima = json.loads(sorted(PROVE.glob("*.json"))[-1].read_text())["riepilogo"]
    except (IndexError, OSError, ValueError, KeyError):
        ultima = None
    byte = sum(f.stat().st_size for f in CASI.rglob("*") if f.is_file()) if CASI.exists() else 0
    return {"casi": len(tutti), "veri": sum(c["vero"] for c in tutti), "per_categoria": per_categoria,
            "da_controllare": {"gemma": sum(c["vero"] for c in dubbie), "qwen": sum(not c["vero"] for c in dubbie)},
            "concordi": {"giusti": sum(c["vero"] for c in concordi), "sbagliati": sum(not c["vero"] for c in concordi)},
            "ultima_prova": ultima, "byte": byte, "cartella": str(BANCO)}


# --------------------------------------------------------------------------
# Prova dei modelli
# --------------------------------------------------------------------------

def prova(modello_esame: str, modello_verifica: str, seconda: str) -> dict:
    """Rifà su tutti i casi ciò che farebbe l'analisi e lo confronta con i giudizi dell'utente."""
    import coordinamento
    import rilevatori
    tutti = casi()
    if not tutti:
        sys.exit("Il banco è vuoto: rivedi qualche segnalazione (errore confermato o falso allarme) e riprova.")
    coordinamento.aspetta_il_turno(lambda t: print(t) if t else None)
    rilevatori.avvia_ollama()
    risultati = {c["cartella"]: {"categoria": c["categoria"], "vero": c["vero"]} for c in tutti}

    def immagini(c):
        return [(Path(c["cartella"]) / n).read_bytes() for n in ("segnato.jpg", "dettaglio.jpg")]

    # 1. Verifica (un modello alla volta in memoria: prima tutta la verifica, poi la seconda opinione e l'esame).
    oggetti = [c for c in tutti if c["categoria"] != "sguardi" and (Path(c["cartella"]) / "dettaglio.jpg").exists()]
    rilevatori.carica_modello(modello_verifica)
    for n, c in enumerate(oggetti, 1):
        try:
            risultati[c["cartella"]]["verifica"] = bool(rilevatori.verifica(modello_verifica, c["categoria"], immagini(c))["errore"])
        except RuntimeError as e:
            risultati[c["cartella"]]["errore_modello"] = str(e)
        print(f"verifica {n}/{len(oggetti)}", end="\r", flush=True)
    coordinamento.libera_modello(modello_verifica, rilevatori.CONTESTO)
    rilevatori.carica_modello(modello_esame)
    for n, c in enumerate(oggetti, 1):
        r = risultati[c["cartella"]]
        if r.get("verifica") and seconda:
            try:
                r["seconda"] = bool(rilevatori.verifica(seconda, c["categoria"], immagini(c))["errore"])
            except RuntimeError as e:
                r["errore_modello"] = str(e)
        print(f"seconda opinione {n}/{len(oggetti)}", end="\r", flush=True)
    # 2. Esame del fotogramma pulito: il modello vede la cosa da segnalare (o il falso allarme)?
    tutte = set(rilevatori.CATEGORIE)
    for n, c in enumerate(tutti, 1):
        f = Path(c["cartella"]) / "fotogramma.jpg"
        if c["categoria"] != "sguardi" and f.exists():
            try:
                risultati[c["cartella"]]["esame"] = any(x["categoria"] == c["categoria"]
                                                       for x in rilevatori.esamina(modello_esame, f, tutte))
                if c["categoria"] == "lavalier":  # lo zoom sul petto della modalità accurata
                    risultati[c["cartella"]]["zoom"] = bool(rilevatori.esamina_zoom(modello_esame, f))
            except RuntimeError as e:
                risultati[c["cartella"]]["errore_modello"] = str(e)
        print(f"esame {n}/{len(tutti)}", end="\r", flush=True)
    coordinamento.libera_modello(modello_esame, rilevatori.CONTESTO)
    # 3. Sguardi: la misura attuale segnalerebbe ancora quel volto?
    sguardi = [c for c in tutti if c["categoria"] == "sguardi" and (Path(c["cartella"]) / "fotogramma.jpg").exists()]
    if sguardi:
        volti = rilevatori.Volti()
        for c in sguardi:
            r = risultati[c["cartella"]]
            trovati = volti.frontali(Path(c["cartella"]) / "fotogramma.jpg")
            vicino = max(trovati, key=lambda v: _sovrapposizione(v["riquadro"], c["riquadro"]), default=None)
            if vicino and _sovrapposizione(vicino["riquadro"], c["riquadro"]) > 0.3:
                r["angolo"] = vicino["angolo"]
                r["sguardo"] = vicino["angolo"] <= rilevatori.soglia_sguardo(vicino["riquadro"])
                r["dubbio"] = r["sguardo"] and not rilevatori.comparsa(vicino["riquadro"])
            else:
                r["sguardo"] = False

    esito = {"quando": time.strftime("%Y-%m-%d %H:%M"), "modelli": {"esame": modello_esame,
             "verifica": modello_verifica, "seconda_opinione": seconda},
             "riepilogo": riepilogo(list(risultati.values())), "casi": risultati}
    PROVE.mkdir(parents=True, exist_ok=True)
    scrivi_json(PROVE / f"{time.strftime('%Y%m%d-%H%M%S')}.json", esito)
    return esito


def _sovrapposizione(a, b) -> float:
    if not a or not b:
        return 0.0
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    unione = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / unione if unione > 0 else 0.0


def riepilogo(risultati: list) -> dict:
    """Per i casi veri: quanti verrebbero segnalati; per i falsi: quanti verrebbero scartati."""
    def conta(filtro, giusto):
        scelti = [r for r in risultati if filtro(r)]
        return {"giusti": sum(1 for r in scelti if giusto(r)), "totale": len(scelti)}

    def esito_oggetto(r):  # "segnalata", "da controllare" o "scartata", come nell'analisi
        if not r.get("verifica"):
            return "scartata"
        return "da controllare" if "seconda" in r and not r["seconda"] else "segnalata"

    oggetti = [r for r in risultati if r["categoria"] != "sguardi" and "verifica" in r]
    sguardi = [r for r in risultati if r["categoria"] == "sguardi"]
    return {
        "verifica_veri_segnalati": conta(lambda r: r in oggetti and r["vero"], lambda r: esito_oggetto(r) == "segnalata"),
        "verifica_veri_da_controllare": conta(lambda r: r in oggetti and r["vero"], lambda r: esito_oggetto(r) == "da controllare"),
        "verifica_falsi_scartati": conta(lambda r: r in oggetti and not r["vero"], lambda r: esito_oggetto(r) == "scartata"),
        "verifica_falsi_da_controllare": conta(lambda r: r in oggetti and not r["vero"], lambda r: esito_oggetto(r) == "da controllare"),
        "esame_veri_visti": conta(lambda r: "esame" in r and r["vero"], lambda r: r["esame"]),
        "esame_falsi_ignorati": conta(lambda r: "esame" in r and not r["vero"], lambda r: not r["esame"]),
        "esame_zoom_veri_visti": conta(lambda r: "zoom" in r and r["vero"], lambda r: r["esame"] or r["zoom"]),
        "esame_zoom_falsi_ignorati": conta(lambda r: "zoom" in r and not r["vero"], lambda r: not (r["esame"] or r["zoom"])),
        "sguardi_veri_segnalati": conta(lambda r: r in sguardi and r["vero"], lambda r: r.get("sguardo")),
        "sguardi_falsi_scartati": conta(lambda r: r in sguardi and not r["vero"], lambda r: not r.get("sguardo")),
        "sguardi_falsi_solo_da_controllare": conta(lambda r: r in sguardi and not r["vero"], lambda r: r.get("dubbio")),
    }


def _stampa_statistiche(s: dict) -> None:
    print(f"Banco di prova: {s['casi']} casi rivisti ({s['veri']} errori confermati), in {s['cartella']}")
    for c in s["per_categoria"].values():
        print(f"  {c['titolo']}: {c['veri']} confermati, {c['falsi']} falsi allarmi")
    d = s["da_controllare"]
    print(f"Da controllare: aveva ragione Gemma {d['gemma']} volte, Qwen3-VL {d['qwen']} volte.")
    k = s["concordi"]
    print(f"Segnalazioni su cui i due modelli erano d'accordo: {k['giusti']} confermate, {k['sbagliati']} false.")


ETICHETTE = {
    "verifica_veri_segnalati": "Errori veri segnalati dalla verifica",
    "verifica_veri_da_controllare": "Errori veri finiti «da controllare»",
    "verifica_falsi_scartati": "Falsi allarmi scartati dalla verifica",
    "verifica_falsi_da_controllare": "Falsi allarmi finiti «da controllare»",
    "esame_veri_visti": "Errori veri visti dall'esame",
    "esame_falsi_ignorati": "Falsi allarmi ignorati dall'esame",
    "esame_zoom_veri_visti": "Lavalier veri visti da esame + zoom sul petto",
    "esame_zoom_falsi_ignorati": "Lavalier falsi ignorati da esame + zoom sul petto",
    "sguardi_veri_segnalati": "Sguardi veri segnalati",
    "sguardi_falsi_scartati": "Sguardi falsi scartati",
    "sguardi_falsi_solo_da_controllare": "Sguardi falsi finiti solo «da controllare»",
}


def main() -> None:
    import rilevatori
    ap = argparse.ArgumentParser(description="Banco di prova di Murnau: statistiche e prova dei modelli.")
    ap.add_argument("azione", nargs="?", default="statistiche", choices=["statistiche", "prova"])
    ap.add_argument("--esame", default=rilevatori.MODELLO_ESAME)
    ap.add_argument("--verifica", default=rilevatori.MODELLO_VERIFICA)
    ap.add_argument("--seconda", default=rilevatori.MODELLO_ESAME, help='"" per non usare la seconda opinione')
    args = ap.parse_args()
    print(f"Raccolte {aggiorna_tutto()} revisioni.")
    if args.azione == "prova":
        esito = prova(args.esame, args.verifica, args.seconda)
        print(f"\nModelli: esame {args.esame}, verifica {args.verifica}, seconda opinione {args.seconda or 'nessuna'}")
        for chiave, v in esito["riepilogo"].items():
            if v["totale"]:
                print(f"  {ETICHETTE[chiave]}: {v['giusti']} su {v['totale']}")
    _stampa_statistiche(statistiche())


if __name__ == "__main__":
    main()
