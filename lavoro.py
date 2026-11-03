"""L'analisi in corso, lanciata dall'app (una sola alla volta).

Avvia murnau.py in un processo a parte, ne legge le righe "@@" (vedi progresso.py) per
fase, avanzamento, percentuale complessiva e tempo restante, e a fine lavoro prepara
l'esito da mostrare: tutto a posto, cose da verificare o un errore spiegato in parole semplici.

Una coda di sessioni (per esempio i rulli di un film) si analizza una dopo l'altra, anche di
notte: a fine coda un solo riquadro riassume com'è andata ciascuna.
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
from pathlib import Path

import coordinamento
import rilevatori
import sessioni
from archivi import scrivi_json

CARTELLA = Path(__file__).resolve().parent
FILE_VELOCITA = sessioni.DATI / "velocita.json"  # secondi per unità di ogni fase, misurati nelle analisi precedenti
FILE_CODA = sessioni.DATI / "coda.json"          # sessioni in attesa di essere analizzate, in ordine


def analizzabile(p: dict) -> None:
    """RuntimeError se la sessione non si può analizzare, con il motivo in parole semplici."""
    if not p.get("video"):
        raise RuntimeError("Scegli prima il film da analizzare.")
    if not Path(p["video"]).exists():
        raise RuntimeError("Il film non si trova più: trascinalo di nuovo nei materiali.")
    if not p.get("categorie"):
        raise RuntimeError("Scegli almeno un tipo di errore da cercare.")


def comando(p: dict) -> list:
    cmd = [sys.executable, "-W", "ignore", str(CARTELLA / "murnau.py"), p["video"], "--out", str(sessioni.cartella_lavoro(p)),
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
        self.nome_sessione = ""
        self.blocco = threading.Lock()
        try:
            self.coda = [x for x in json.loads(FILE_CODA.read_text()) if isinstance(x, str)]
        except (OSError, ValueError):
            self.coda = []
        self.in_coda = False  # la coda sta andando: a fine analisi parte la prossima
        self.esiti_coda = []  # (nome della sessione, esito) delle analisi della coda in corso

    def attivo(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    def avvia(self, cmd: list, sessione: str, nome: str = "") -> None:
        with self.blocco:
            if self.attivo():
                raise RuntimeError("C'è già un'analisi in corso.")
            fuori = coordinamento.analisi_in_corso()
            if fuori:
                raise RuntimeError("C'è già un'analisi di Murnau in corso, avviata fuori dall'app: due analisi "
                                   "insieme si rubano il modello a vicenda. Fermala o aspetta che finisca.")
            self.fase, self.avanz, self.piano, self.codice, self.sessione = "avvio", {}, {}, None, sessione
            self.nome_sessione = nome
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
        if self.fermata:
            self.in_coda = False  # Ferma ferma anche la coda: le sessioni non ancora analizzate restano in coda
            return
        esito = esito_del_lavoro(self)
        if not self.in_coda:
            self.esito = esito
            avvisa_fine(esito)
            return
        self.esiti_coda.append((self.nome_sessione, esito))
        self.prossima_in_coda()

    # ---------------- coda ----------------

    def _salva_coda(self) -> None:
        try:
            FILE_CODA.parent.mkdir(parents=True, exist_ok=True)
            scrivi_json(FILE_CODA, self.coda)
        except OSError:
            pass

    def metti_in_coda(self, p: dict) -> None:
        analizzabile(p)
        if p["id"] not in self.coda and not (self.attivo() and self.sessione == p["id"]):
            self.coda.append(p["id"])
            self._salva_coda()

    def togli_dalla_coda(self, id_: str) -> None:
        if id_ in self.coda:
            self.coda.remove(id_)
            self._salva_coda()

    def avvia_coda(self) -> None:
        if self.attivo():
            raise RuntimeError("C'è già un'analisi in corso: la coda partirà da sola alla fine.") if self.in_coda \
                else RuntimeError("C'è un'analisi in corso: aspetta che finisca, o fermala, prima di avviare la coda.")
        if not self.coda:
            raise RuntimeError("La coda è vuota: aggiungi le sessioni con «Metti in coda».")
        self.in_coda, self.esiti_coda, self.esito = True, [], None
        self.prossima_in_coda()

    def prossima_in_coda(self) -> None:
        """Avvia la prossima sessione della coda; se non ce ne sono più, prepara il riepilogo."""
        while self.coda:
            id_ = self.coda.pop(0)
            self._salva_coda()
            nome = id_
            try:
                p = sessioni.leggi_sessione(id_)
                nome = p.get("nome") or id_
                analizzabile(p)
                self.avvia(comando(p), p["id"], nome)
                return
            except (OSError, ValueError, RuntimeError) as e:
                # Una sessione che non parte (film spostato, sessione eliminata) non ferma le altre.
                self.esiti_coda.append((nome, {"stato": "errore", "punti": [f"Motivo: {e}"], "segnalazioni": None}))
        self.in_coda = False
        self.esito = riepilogo_coda(self.esiti_coda)
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
            FILE_VELOCITA.parent.mkdir(parents=True, exist_ok=True)
            scrivi_json(FILE_VELOCITA, self.velocita)
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

    def occupa(self, id_sessione: str) -> bool:
        """True se un'analisi in corso sta lavorando su quella sessione."""
        return self.attivo() and self.sessione == id_sessione

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
                "sessione": self.sessione, "nome_sessione": self.nome_sessione,
                "coda": self.elenco_coda(), "in_coda": self.in_coda, "fatte_in_coda": len(self.esiti_coda), "durata": time.time() - self.inizio if self.inizio else 0,
                "log": list(self.log)[-40:]}


    def elenco_coda(self) -> list:
        elenco = []
        for id_ in self.coda:
            try:
                p = sessioni.leggi_sessione(id_)
                elenco.append({"id": id_, "nome": p.get("nome", ""), "video": Path(p.get("video") or "").name})
            except (OSError, ValueError):
                elenco.append({"id": id_, "nome": "(sessione non leggibile)", "video": ""})
        return elenco


LAVORO = Lavoro()


def riepilogo_coda(esiti: list) -> dict:
    """Un solo esito per tutta la coda: una riga per sessione, lo stato peggiore in testa."""
    peso = {"ok": 0, "verifica": 1, "errore": 2}
    punti = []
    for nome, e in esiti:
        if e["stato"] == "errore":
            motivo = next((x[len("Motivo: "):] for x in e["punti"] if x.startswith("Motivo: ")), "errore")
            punti.append(f"{nome}: interrotta. {motivo}")
        else:
            n = e.get("segnalazioni")
            riga = f"{nome}: " + ("analizzata" if n is None else
                                  "nessuna segnalazione" if n == 0 else
                                  "1 segnalazione" if n == 1 else f"{n} segnalazioni")
            if e["punti"]:
                riga += " · " + " ".join(e["punti"])
            punti.append(riga)
    stato = max((e["stato"] for _, e in esiti), key=peso.get, default="ok")
    totale = sum(e.get("segnalazioni") or 0 for _, e in esiti)
    quante = len(esiti)
    return {"stato": stato, "coda": True, "punti": punti, "segnalazioni": totale,
            "titolo": f"Coda finita: {quante} {'sessione analizzata' if quante == 1 else 'sessioni analizzate'}"}


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
        segnalazioni = sessioni.numero_segnalazioni(sessioni.leggi_sessione(l.sessione))
    except (OSError, ValueError):
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

