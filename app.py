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
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aggiornamenti
import coordinamento
import esporta
import pulizia
import rilevatori
import sessioni
from archivi import scrivi_json
from lavoro import LAVORO, comando
from sessioni import (DATI, RUOLI, _lavoro, aggiungi_materiali, assegna, carica_progetto, cartella_lavoro,
                      materiali_di, nome_per_file, salva_progetto, togli_materiale)

CARTELLA = Path(__file__).resolve().parent
PORTA = 8766
REGISTRO = DATI / "murnau.log"
MODELLO = rilevatori.MODELLO_PREDEFINITO
TIPI_IMMAGINE = {".jpg": "image/jpeg", ".png": "image/png"}


# --------------------------------------------------------------------------
# Controlli che costano (processi in corso, Ollama): l'interfaccia chiede lo stato ogni 1,5 secondi,
# ma basta rifarli ogni pochi secondi.
# --------------------------------------------------------------------------

_RECENTI, _BLOCCO_RECENTI = {}, threading.Lock()


def recente(chiave: str, secondi: float, calcola):
    with _BLOCCO_RECENTI:
        valore, quando = _RECENTI.get(chiave, (None, 0.0))
        if time.monotonic() - quando < secondi:
            return valore
    valore = calcola()
    with _BLOCCO_RECENTI:
        _RECENTI[chiave] = (valore, time.monotonic())
    return valore


def stato_ollama() -> dict:
    installati = rilevatori._modelli_installati()
    return {"ollama": installati is not None,
            "modello": any(nome.startswith(MODELLO) for nome in installati or [])}


def versione_segnalazioni(out: Path) -> str:
    """Cambia quando cambiano le segnalazioni o la revisione: l'interfaccia le ricarica solo allora."""
    firma = [str(out)]
    for nome in ("segnalazioni.json", "revisione.json"):
        try:
            firma.append(str((out / nome).stat().st_mtime_ns))
        except OSError:
            firma.append("")
    return "|".join(firma) if firma[1] else ""


# --------------------------------------------------------------------------
# Segnalazioni e revisione
# --------------------------------------------------------------------------

def leggi_segnalazioni(out: Path) -> dict:
    if not (out / "segnalazioni.json").exists():
        return {"segnalazioni": [], "revisione": {}}
    dati, revisione = esporta.leggi(out)
    return {**dati, "revisione": revisione}


# Una revisione alla volta: due clic ravvicinati (o un'esportazione durante un clic) non devono
# leggere lo stesso revisione.json e poi sovrascriversi a vicenda.
BLOCCO_REVISIONE = threading.Lock()


def rivedi(out: Path, ident: str, stato: str = None, nota: str = None) -> None:
    with BLOCCO_REVISIONE:
        _rivedi(out, ident, stato, nota)


def _rivedi(out: Path, ident: str, stato: str, nota: str) -> None:
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
    scrivi_json(out / "revisione.json", revisione)
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
    if not formati:
        raise RuntimeError("Scegli almeno un tipo di file da esportare.")
    if not p.get("out"):
        raise RuntimeError("Scegli prima la cartella in cui esportare.")
    lavoro = _lavoro(p)
    if not (lavoro / "segnalazioni.json").exists():
        raise RuntimeError("Non c'è ancora niente da esportare: prima analizza il film.")
    with BLOCCO_REVISIONE:
        return _esporta_file(p, formati, lavoro)


def _esporta_file(p: dict, formati: list, lavoro: Path) -> list:
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
            for a in recente("analisi", 5, coordinamento.analisi_in_corso):
                if a["pid"] == nostra:
                    continue
                esterne.append({"pid": a["pid"], "film": Path(a["video"]).name if a.get("video") else "",
                                "origine": a.get("origine", "sconosciuta"),
                                "da": time.time() - a["avviata"] if a.get("avviata") else None,
                                "fase": a.get("fase", ""), "fatti": a.get("fatti", 0), "totale": a.get("totale", 0),
                                "attesa": a.get("attesa", ""), "out": a.get("out", "")})
            versione = versione_segnalazioni(out)
            self._json({"progetto": p, "lavoro": LAVORO.stato(), "mancanti": mancanti, "esterne": esterne,
                        **recente("ollama", 10, stato_ollama),
                        "risultati": bool(versione), "esportabili": bool(versione), "versione": versione,
                        "categorie": rilevatori.CATEGORIE})
        elif url.path == "/api/sessioni":
            self._json({"corrente": p["id"], "sessioni": sessioni.elenco_sessioni()})
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
                    p = sessioni.nuova_sessione(corpo.get("nome", ""))
                elif azione == "apri":
                    p = sessioni.leggi_sessione(corpo["id"])
                    salva_progetto(p)
                elif azione == "elimina":
                    if LAVORO.occupa(corpo["id"]):
                        raise RuntimeError("Questa sessione ha un'analisi in corso: fermala prima di eliminarla.")
                    esito = sessioni.elimina_sessione(corpo["id"], bool(corpo.get("risultati")))
                    self._json({"ok": True, "progetto": carica_progetto(), **esito})
                    return
                elif azione == "salva":
                    if corpo.get("nome", "").strip():
                        p["nome"] = corpo["nome"].strip()
                        sessioni.rinomina_cartella_lavoro(p, in_uso=LAVORO.occupa(p["id"]))
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
                _RECENTI.pop("analisi", None)
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
    sessioni.trasloca_tutte()
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
