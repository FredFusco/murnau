"""Test di Murnau che non usano modelli né Ollama: girano in pochi secondi.

    ~/.venvs/descrittore/bin/python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

CARTELLA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CARTELLA))

import app  # noqa: E402
import banco  # noqa: E402
import esporta  # noqa: E402
import lavoro  # noqa: E402
import montaggio  # noqa: E402
import murnau  # noqa: E402
import rilevatori  # noqa: E402
import sessioni  # noqa: E402
from archivi import Archivio, scrivi_json  # noqa: E402


class ConCartella(unittest.TestCase):
    """Ogni test lavora in una cartella temporanea, con sessioni e cartelle di lavoro proprie."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="murnau-test-"))
        self._originali = {k: getattr(sessioni, k) for k in ("SESSIONI", "FILE_CORRENTE", "FILE_VECCHIO", "FILE_DI_LAVORO")}
        sessioni.SESSIONI = self.d / "sessioni"
        sessioni.FILE_CORRENTE = self.d / "sessione_corrente.json"
        sessioni.FILE_VECCHIO = self.d / "progetto.json"
        sessioni.FILE_DI_LAVORO = self.d / "Murnau – file di lavoro"
        # Le revisioni dei test non devono finire nel banco di prova vero.
        self._banco = (banco.CASI, banco.PROVE)
        banco.CASI, banco.PROVE = self.d / "banco" / "casi", self.d / "banco" / "prove"

    def tearDown(self):
        for k, v in self._originali.items():
            setattr(sessioni, k, v)
        banco.CASI, banco.PROVE = self._banco
        shutil.rmtree(self.d, ignore_errors=True)


def segnalazioni_finte(cartella: Path, n: int) -> list:
    segn = [{"id": f"{i:05d}-troupe", "numero": i, "categoria": "troupe", "titolo": "Troupe", "descrizione": "d",
             "certezza": "alta", "tc": "01:00:00:00", "tc_in": "01:00:00:00", "tc_out": "01:00:01:00", "clip": "",
             "scena": "", "immagine": "segnalazioni/x.jpg", "dettaglio": "segnalazioni/x.jpg",
             "fotogrammi_sospetti": 1, "fotogrammi_totali": 1} for i in range(n)]
    (cartella / "segnalazioni").mkdir(parents=True, exist_ok=True)
    (cartella / "segnalazioni" / "x.jpg").write_bytes(b"\xff\xd8finto\xff\xd9")
    (cartella / "segnalazioni.json").write_text(json.dumps(
        {"video": "v.mov", "fps": 24, "inquadrature": n, "creato": "oggi", "segnalazioni": segn}))
    return segn


class Timecode(unittest.TestCase):
    def test_andata_e_ritorno(self):
        for tc in ("00:00:00:00", "01:13:13:11", "10:59:59:23"):
            self.assertEqual(montaggio.secondi_a_tc(montaggio.tc_a_secondi(tc, 24), 24), tc)

    def test_drop_frame(self):
        # Al minuto 1 il drop frame salta le etichette ;00 e ;01: 00:01:00;02 è il fotogramma 1800.
        self.assertAlmostEqual(montaggio.tc_a_secondi("00:01:00;02", 29.97), 1800 / 29.97, places=6)


class Montaggio(unittest.TestCase):
    def test_edl(self):
        with tempfile.TemporaryDirectory() as d:
            edl = Path(d) / "prova.edl"
            edl.write_text(
                "TITLE: prova\nFCM: NON-DROP FRAME\n\n"
                "000001  A001  V     C        10:00:00:00 10:00:02:00 01:00:00:00 01:00:02:00\n"
                "* FROM CLIP NAME:  CLIP UNO\n"
                "000002  A002  A     C        10:00:00:00 10:00:05:00 01:00:00:00 01:00:05:00\n"
                "000003  A003  V     C        11:00:00:00 11:00:03:00 01:00:02:00 01:00:05:00\n")
            inq = montaggio.inquadrature_da_eventi(montaggio.leggi_edl(edl), 24, montaggio.tc_a_secondi("01:00:00:00", 24))
            self.assertEqual([(q.numero, q.clip, round(q.inizio, 3), round(q.fine, 3)) for q in inq],
                             [(1, "CLIP UNO", 0.0, 2.0), (2, "A003", 2.0, 5.0)])


class ArchivioRipresa(unittest.TestCase):
    def test_riga_troncata(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.jsonl"
            f.write_text('{"k": "uno", "v": 1}\n{"k": "due", "v"')  # interrotto a metà scrittura
            a = Archivio(f)
            self.assertIn("uno", a)
            self.assertNotIn("due", a)
            a.metti("tre", 3)
            self.assertEqual(Archivio(f)["tre"], 3)
            self.assertEqual(Archivio(f)["uno"], 1)


class ScritturaSicura(unittest.TestCase):
    def test_un_errore_lascia_il_file_vecchio(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            scrivi_json(f, {"a": 1})
            with self.assertRaises(TypeError):
                scrivi_json(f, {"a": object()})  # non si può scrivere: a metà
            self.assertEqual(json.loads(f.read_text()), {"a": 1})
            self.assertEqual([x.name for x in Path(d).iterdir()], ["s.json"], "è rimasto un file temporaneo")


class Nomi(unittest.TestCase):
    def test_nome_per_file(self):
        self.assertEqual(sessioni.nome_per_file("Scena 1/2: notte"), "Scena 1-2- notte")
        self.assertEqual(sessioni.nome_per_file("  ..  "), "Sessione")


class Revisione(ConCartella):
    def test_clic_simultanei_non_si_perdono(self):
        segn = segnalazioni_finte(self.d, 40)
        fili = [threading.Thread(target=app.rivedi, args=(self.d, s["id"], "falso")) for s in segn]
        for f in fili:
            f.start()
        for f in fili:
            f.join()
        rev = json.loads((self.d / "revisione.json").read_text())
        self.assertEqual(len([k for k in rev if not k.startswith("impronta:")]), 40)

    def test_rianalisi_non_eredita_giudizi_altrui(self):
        """Dopo una nuova analisi, il giudizio vale solo se la segnalazione è la stessa."""
        segnalazioni_finte(self.d, 2)
        app.rivedi(self.d, "00000-troupe", "falso")
        app.rivedi(self.d, "00001-troupe", "confermato")
        dati = json.loads((self.d / "segnalazioni.json").read_text())
        dati["segnalazioni"][0]["riquadro"] = [0.1, 0.1, 0.2, 0.2]   # stessa inquadratura, altra cosa
        (self.d / "segnalazioni.json").write_text(json.dumps(dati))
        _, rev = esporta.leggi(self.d)
        self.assertNotIn("00000-troupe", rev, "un vecchio falso allarme vale per una segnalazione diversa")
        self.assertEqual(rev["00001-troupe"], "confermato")

    def test_giudizi_delle_versioni_precedenti(self):
        """I giudizi senza impronta prendono quella della segnalazione vista dall'utente prima della nuova analisi."""
        segnalazioni_finte(self.d, 1)
        (self.d / "revisione.json").write_text(json.dumps({"00000-troupe": "falso"}))
        esporta.fissa_revisione(self.d)
        dati = json.loads((self.d / "segnalazioni.json").read_text())
        dati["segnalazioni"][0]["tc"] = "01:00:09:00"
        (self.d / "segnalazioni.json").write_text(json.dumps(dati))
        self.assertNotIn("00000-troupe", esporta.leggi(self.d)[1])

    def test_nota_e_ripristino(self):
        segnalazioni_finte(self.d, 2)
        app.rivedi(self.d, "00000-troupe", "falso", "un lampione")
        app.rivedi(self.d, "00000-troupe", "")
        rev = json.loads((self.d / "revisione.json").read_text())
        self.assertNotIn("00000-troupe", rev)
        self.assertEqual(rev["note:00000-troupe"], "un lampione")


class Sessioni(ConCartella):
    def test_lettura_momentaneamente_fallita(self):
        """La sessione aperta per un istante non si legge (un altro programma la sta scrivendo):
        si riprova e si riapre lei, senza creare una sessione nuova."""
        p = sessioni.nuova_sessione("Mia")
        f = sessioni._file_sessione(p["id"])
        buono = f.read_text()
        f.write_text(buono[:20])
        threading.Timer(0.15, f.write_text, args=(buono,)).start()
        q = sessioni.carica_progetto()
        self.assertEqual(q["id"], p["id"])
        self.assertEqual(len(list(sessioni.SESSIONI.glob("*.json"))), 1, "è comparsa una sessione nuova")

    def test_sessione_rovinata_apre_la_piu_recente(self):
        altra = sessioni.nuova_sessione("Altra")
        p = sessioni.nuova_sessione("Rovinata")
        sessioni._file_sessione(p["id"]).write_text('{"id": "')
        sessioni.time.sleep, originale = (lambda _: None), sessioni.time.sleep
        try:
            q = sessioni.carica_progetto()
        finally:
            sessioni.time.sleep = originale
        self.assertEqual(q["id"], altra["id"])
        self.assertEqual(len(list(sessioni.SESSIONI.glob("*.json"))), 2, "è comparsa una sessione nuova")

    def test_sposta_solo_i_file_di_murnau(self):
        out = self.d / "risultati"
        segnalazioni_finte(out, 1)
        (out / "lavoro").mkdir()
        (out / "marker_resolve.edl").write_text("TITLE: Descrizioni inquadrature\n")  # è di Vertov
        (out / "film.mov").write_text("materiale")
        p = sessioni.nuova_sessione("Prova", {"out": str(out), "video": str(out / "film.mov")})
        nomi = [f.name for f, _ in sessioni.da_traslocare(p)]
        self.assertIn("segnalazioni.json", nomi)
        self.assertIn("lavoro", nomi)
        self.assertNotIn("marker_resolve.edl", nomi)
        self.assertNotIn("film.mov", nomi)

    def test_rinomina_della_cartella_di_lavoro(self):
        p = sessioni.nuova_sessione("Prima")
        vecchia = sessioni.cartella_lavoro(p)
        p["nome"] = "Dopo"
        sessioni.rinomina_cartella_lavoro(p, in_uso=True)
        self.assertTrue(vecchia.exists(), "rinominata mentre un'analisi ci scriveva")
        sessioni.rinomina_cartella_lavoro(p)
        self.assertFalse(vecchia.exists())
        self.assertEqual(Path(p["cartella_lavoro"]).name, "Dopo")


class Esito(unittest.TestCase):
    def test_errori_in_parole_semplici(self):
        self.assertIn("Ollama non risponde", lavoro._ultimo_errore(["...", "urllib.error.URLError: <urlopen error [Errno 61]>"]))
        self.assertIn("non corrisponde al film", lavoro._ultimo_errore(["Il montaggio non corrisponde al video: 3 su 3"]))
        self.assertIn("disco è pieno", lavoro._ultimo_errore(["OSError: [Errno 28] No space left on device"]))

    def test_percentuale_non_torna_indietro(self):
        l = lavoro.Lavoro()
        l.velocita = {}
        ora = time.time()
        l.piano = {"esame": (10, 4.5), "verifica": (10, 3.5)}
        l.fase = "verifica"
        l.inizio_fase = ora - 60
        l.avanz = {"esame": {"fatti": 10, "totale": 10, "primo": (1, ora - 120), "ora": ora - 80},
                   "verifica": {"fatti": 8, "totale": 10, "primo": (1, ora - 40), "ora": ora - 5}}
        prima = l.complessivo()["percentuale"]
        l.piano["verifica"] = (40, 3.5)  # la verifica si rivela molto più lunga
        l.avanz["verifica"]["totale"] = 40
        self.assertGreaterEqual(l.complessivo()["percentuale"], prima)


class Ollama(unittest.TestCase):
    def test_connessione_chiusa_a_meta(self):
        """Ollama che chiude la connessione senza rispondere: errore del modello, non un crash."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)

        def servi():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                c.recv(65536)
                c.close()

        threading.Thread(target=servi, daemon=True).start()
        originali = rilevatori.OLLAMA_URL, rilevatori.time.sleep
        rilevatori.OLLAMA_URL = f"http://127.0.0.1:{s.getsockname()[1]}/api/chat"
        rilevatori.time.sleep = lambda _: None
        try:
            with self.assertRaises(RuntimeError):
                rilevatori.chiedi("m", "ciao", [], {"type": "object"}, attesa_ollama=0)
        finally:
            rilevatori.OLLAMA_URL, rilevatori.time.sleep = originali
            s.close()


class ModelliSenzaSchema(unittest.TestCase):
    """Gemma 4 e gli altri modelli col motore MLX di Ollama non accettano la risposta a schema (501):
    il JSON si chiede nel testo e la risposta si rende conforme allo schema."""

    def test_ripiego_sul_testo(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        richieste = []

        class Finto(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                richieste.append(corpo)
                if "format" in corpo:
                    self.send_response(501)
                    self.end_headers()
                    self.wfile.write(b'{"error":"structured output is unavailable"}')
                    return
                testo = '```json\n{"cosa_vedo": "un microfono", "classe": "microfono in mano"}\n```'
                risposta = json.dumps({"message": {"content": testo}, "done_reason": "stop"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(risposta)))
                self.end_headers()
                self.wfile.write(risposta)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Finto)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        originale = rilevatori.OLLAMA_URL
        rilevatori.OLLAMA_URL = f"http://127.0.0.1:{server.server_address[1]}/api/chat"
        schema = {"type": "object", "properties": {"cosa_vedo": {"type": "string"},
                  "classe": {"type": "string", "enum": ["microfono tenuto in mano (di scena)", "microfono boom"]}}}
        try:
            r = rilevatori.chiedi("finto:1b", "Che cosa vedi?", [], schema, attesa_ollama=0)
        finally:
            rilevatori.OLLAMA_URL = originale
            server.shutdown()
            rilevatori.SENZA_SCHEMA.discard("finto:1b")
        self.assertEqual(r["classe"], "microfono tenuto in mano (di scena)")  # l'opzione più simile
        self.assertEqual(len(richieste), 2)
        self.assertIn("SOLO con un oggetto JSON", richieste[1]["messages"][0]["content"])

    def test_risposta_resa_conforme(self):
        schema = {"type": "object", "properties": {"voce": {"type": "object", "properties": {
            "presente": {"type": "boolean"}, "riquadro": {"type": "array", "items": {"type": "integer"}}}}}}
        self.assertEqual(rilevatori._secondo_schema({"voce": {"presente": "true"}}, schema),
                         {"voce": {"presente": True, "riquadro": []}})
        opzioni = ["oggetto non riconoscibile", "microfono tenuto in mano (di scena)", "microfono boom"]
        self.assertEqual(rilevatori._opzione_piu_vicina("un boh qualunque", opzioni), "oggetto non riconoscibile")
        self.assertEqual(rilevatori._opzione_piu_vicina("microfono", opzioni), "oggetto non riconoscibile")  # ambiguo
        self.assertEqual(rilevatori._opzione_piu_vicina("Microfono Boom.", opzioni), "microfono boom")
        self.assertEqual(rilevatori._opzione_piu_vicina("forse", ["alta", "media", "bassa"]), "bassa")


class ModelliDelleAltreApp(unittest.TestCase):
    def test_libera_spazio_non_tocca_i_modelli_di_vertov_e_ozu(self):
        """I modelli delle app sorelle si leggono sia dai sorgenti in My Drive/CLAUDE sia dai programmi
        dentro le app installate in ~/Applications."""
        import pulizia
        with tempfile.TemporaryDirectory() as d:
            sorgenti, applicazioni = Path(d) / "CLAUDE", Path(d) / "Applications"
            (sorgenti / "ozu").mkdir(parents=True)
            (sorgenti / "ozu" / "motore.py").write_text('MODELLO = "gemma9:12b"\nREVISORE = "qwen9.8:27b"\n')
            programma = applicazioni / "Vertov.app" / "Contents" / "Resources" / "programma"
            programma.mkdir(parents=True)
            (programma / "modello.py").write_text('    MODELLO_DESCRIZIONI = "gemma9:26b"\nx = "altro:1b"\n'
                                                  'INDIRIZZO = "http://localhost:11434"\n')
            originali = pulizia.CODICE_FAMIGLIA, pulizia.APP_INSTALLATE
            pulizia.CODICE_FAMIGLIA, pulizia.APP_INSTALLATE = sorgenti, applicazioni
            try:
                protetti = pulizia.modelli_protetti()
            finally:
                pulizia.CODICE_FAMIGLIA, pulizia.APP_INSTALLATE = originali
        self.assertTrue({"gemma9:12b", "qwen9.8:27b", "gemma9:26b", *rilevatori.MODELLI} <= protetti)
        self.assertNotIn("altro:1b", protetti)
        self.assertFalse(any("localhost" in m for m in protetti))


class ProcessiDelleAppSorelle(unittest.TestCase):
    """Le analisi delle app si riconoscono dal nome del file, con o senza percorso: ora girano da dentro le app."""

    def test_con_e_senza_percorso(self):
        import coordinamento as c
        app = "/Users/x/Applications/Vertov.app/Contents/Resources/programma/descrivi.py"
        self.assertTrue(c._esegue(f"/opt/python -u {app} film.mov --out y", "descrivi.py"))
        self.assertTrue(c._esegue("python -u descrivi.py film.mov", "descrivi.py"))
        self.assertTrue(c._esegue("python /Users/x/My Drive/CLAUDE/vertov/descrivi.py", "descrivi.py"))
        self.assertTrue(c._esegue("python -W ignore /Users/x/Applications/Murnau.app/Contents/Resources/programma/"
                                  "murnau.py /film.mov --out /lavoro", c.PROGRAMMA))
        self.assertFalse(c._esegue("python ridescrivi.py", "descrivi.py"))
        self.assertFalse(c._esegue("python app.py --servizio", c.PROGRAMMA))
        self.assertFalse(c._esegue("python murnau.pyc", c.PROGRAMMA))


class SceltaDeiFile(unittest.TestCase):
    def test_percorsi_dalla_finestra_nativa(self):
        self.assertEqual(app.scegli("file", True, {"percorsi": ["/a/film.mov", "/a/m.aaf"]}), ["/a/film.mov", "/a/m.aaf"])
        self.assertEqual(app.scegli("cartella", False, {"percorsi": ["/a/esporta/"]}), "/a/esporta")
        self.assertIsNone(app.scegli("file", True, {"percorsi": []}))  # annullata

    def test_in_servizio_senza_percorsi(self):
        originale, app.SERVIZIO = app.SERVIZIO, True
        try:
            with self.assertRaises(RuntimeError):
                app.scegli("file", True, {})
        finally:
            app.SERVIZIO = originale


class Servizio(unittest.TestCase):
    """app.py --servizio: solo il server, con dati e sessioni in una casa finta (mai quelle vere)."""

    def _avvia(self, casa):
        import subprocess
        return subprocess.Popen([sys.executable, "-W", "ignore", str(CARTELLA / "app.py"), "--servizio"], cwd=CARTELLA,
                                env={**os.environ, "HOME": str(casa), "PYTHONDONTWRITEBYTECODE": "1"},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_porta_occupata(self):
        occupata = socket.socket()
        try:
            occupata.bind(("127.0.0.1", app.PORTA))
            occupata.listen(1)
        except OSError:
            occupata.close()  # la occupa qualcun altro, che potrebbe liberarla a metà prova
            self.skipTest("la porta di Murnau è occupata (Murnau è aperto)")
        p = None
        try:
            with tempfile.TemporaryDirectory() as casa:
                p = self._avvia(casa)
                self.assertEqual(p.wait(timeout=60), app.CODICE_PORTA_OCCUPATA)
        finally:
            occupata.close()
            if p is not None and p.poll() is None:
                p.kill()

    def test_risponde_e_si_ferma(self):
        import signal
        import urllib.request
        prova = socket.socket()
        try:
            prova.bind(("127.0.0.1", app.PORTA))
        except OSError:
            self.skipTest("la porta di Murnau è occupata (Murnau è aperto)")
        finally:
            prova.close()
        with tempfile.TemporaryDirectory() as casa:
            p = self._avvia(casa)
            try:
                limite = time.time() + 60
                while True:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{app.PORTA}/api/sessioni", timeout=1) as r:
                            self.assertEqual(r.status, 200)
                        break
                    except OSError:
                        self.assertLess(time.time(), limite, "il server non risponde")
                        self.assertIsNone(p.poll(), p.stdout.read() if p.poll() is not None else "")
                        time.sleep(0.2)
                p.send_signal(signal.SIGTERM)
                self.assertEqual(p.wait(timeout=10), 0)
                self.assertTrue((Path(casa) / "Library" / "Application Support" / "Murnau" / "sessioni").exists(),
                                "le sessioni non sono nella casa finta")
            finally:
                if p.poll() is None:
                    p.kill()


class ScambioDellApp(unittest.TestCase):
    """Un aggiornamento scambia Murnau.app intera con quella del pacchetto; la vecchia va in versioni_precedenti."""

    def test_zip_finto(self):
        import zipfile
        import aggiornamenti as ag
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installata = d / "Applications" / "Murnau.app"
            (installata / "Contents" / "MacOS").mkdir(parents=True)
            (installata / "Contents" / "MacOS" / "Murnau").write_text("vecchia")
            pacchetto = d / "murnau.zip"
            with zipfile.ZipFile(pacchetto, "w") as z:
                eseguibile = zipfile.ZipInfo("Murnau.app/Contents/MacOS/Murnau")
                eseguibile.external_attr = 0o755 << 16
                z.writestr(eseguibile, "nuova")
                z.writestr("Murnau.app/Contents/Resources/programma/app.py", "# programma")
            originali = ag.APP, ag.PRECEDENTI
            ag.APP, ag.PRECEDENTI = installata, d / "versioni_precedenti"
            try:
                ag.scambia_app(pacchetto, d / "estratti")
            finally:
                ag.APP, ag.PRECEDENTI = originali
            nuova = installata / "Contents" / "MacOS" / "Murnau"
            self.assertEqual(nuova.read_text(), "nuova")
            self.assertTrue(os.access(nuova, os.X_OK), "l'eseguibile ha perso i permessi")
            self.assertEqual((d / "versioni_precedenti" / ag.versione() / "Murnau.app" / "Contents" / "MacOS" / "Murnau")
                             .read_text(), "vecchia")
            self.assertFalse((d / "Applications" / ".Murnau.app.nuova").exists())

    def test_pacchetto_sbagliato(self):
        import zipfile
        import aggiornamenti as ag
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pacchetto = d / "murnau.zip"
            with zipfile.ZipFile(pacchetto, "w") as z:
                z.writestr("altro/file.txt", "x")
            originali = ag.APP
            ag.APP = d / "Murnau.app"
            try:
                with self.assertRaises(RuntimeError):
                    ag.scambia_app(pacchetto, d / "estratti")
            finally:
                ag.APP = originali

    def test_dal_codice_sorgente_non_si_installa(self):
        import aggiornamenti as ag
        self.assertIsNone(ag.APP)  # i test girano dal codice sorgente
        with self.assertRaises(RuntimeError) as e:
            ag._installa_app({"nuova": "9.9.9", "pacchetto": "https://x/y.zip", "sha256": "0"})
        self.assertIn("compila.sh --installa", str(e.exception))


class BandeNere(unittest.TestCase):
    def test_scritte_nelle_bande(self):
        """Letterbox con scritte bianche nelle bande: si tagliano bande e scritte, non l'immagine."""
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "v.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=640x360:rate=24:duration=3", "-vf",
                            "drawbox=0:0:640:60:black:fill,drawbox=0:300:640:60:black:fill,"
                            "drawbox=20:20:120:14:white:fill,drawbox=400:320:90:14:white:fill",
                            "-pix_fmt", "yuv420p", str(video)], check=True)
            ritaglio = montaggio.rileva_bande_nere(video, 3.0)
        w, h, x, y = (int(v) for v in ritaglio.split("=")[1].split(":"))
        self.assertTrue(60 <= y <= 64 and 296 <= y + h <= 300 and w >= 630, ritaglio)


class DueLivelli(unittest.TestCase):
    """Ciò che la verifica accetta passa dalla seconda opinione: d'accordo = segnalazione, no = da controllare."""

    def test_verifica_e_seconda_opinione(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            args = murnau.leggi_argomenti(["v.mov", "--out", d, "--categorie", "troupe,lavalier",
                                           "--modello", "esame:1b", "--modello-verifica", "verifica:1b",
                                           "--seconda-opinione", "esame:1b"])
            a = murnau.Analisi(args)
            a.lavoro.mkdir(parents=True)
            a.fps, a.tc_inizio_s = 24.0, 0.0
            q = types.SimpleNamespace(numero=1, inizio=0.0, fine=2.0, tc_in="00:00:00:00", tc_out="00:00:02:00",
                                      clip="", scena=lambda: "")
            a.inquadrature = [q]
            a.fotogrammi = {1: [(1.0, Path(d) / "f000001.jpg")]}
            a.archivi()
            a.esame.metti(f"f000001.jpg|{a.chiave_cat}", [
                {"categoria": "troupe", "voce": "boom", "descrizione": "", "riquadro": [0.1, 0.1, 0.2, 0.2]},
                {"categoria": "lavalier", "voce": "lavalier", "descrizione": "", "riquadro": [0.5, 0.5, 0.6, 0.6]}])
            chiamate = []

            def verifica(modello, cat, immagini):
                chiamate.append((modello, cat))
                d_accordo = modello == "verifica:1b" or cat == "troupe"  # la seconda opinione nega il lavalier
                return {"errore": d_accordo, "cosa_vedo": f"{modello} {cat}", "classe": "x", "certezza": "alta"}

            originali = (murnau.rilevatori.verifica, murnau.passa_a, murnau.turno, murnau.immagini_sospetto)
            murnau.rilevatori.verifica, murnau.passa_a, murnau.turno = verifica, lambda *x: None, lambda: None
            murnau.immagini_sospetto = lambda *x: (b"jpg", b"jpg")
            try:
                elenco = murnau.verifica_sospetti(a)
                rifatte = len(chiamate)
                murnau.verifica_sospetti(a)  # ripresa: tutto è già in archivio
            finally:
                murnau.rilevatori.verifica, murnau.passa_a, murnau.turno, murnau.immagini_sospetto = originali
        per_cat = {x["categoria"]: x for x in elenco}
        self.assertFalse(per_cat["troupe"].get("da_controllare"))
        self.assertTrue(per_cat["lavalier"]["da_controllare"])
        self.assertEqual(per_cat["lavalier"]["certezza"], "bassa")
        self.assertEqual(per_cat["lavalier"]["seconda_opinione"], "esame:1b lavalier")
        self.assertEqual(rifatte, 4)
        self.assertEqual(len(chiamate), 4, "alla ripresa le verifiche sono state rifatte")


class Coda(ConCartella):
    """Più sessioni analizzate una dopo l'altra; una che non parte non ferma le altre; un solo riepilogo."""

    def test_coda_di_tre_sessioni(self):
        import subprocess as sp
        originali = (lavoro.comando, lavoro.avvisa_fine, lavoro.FILE_CODA, lavoro.coordinamento.analisi_in_corso)
        avvisi = []
        lavoro.comando = lambda p: [sys.executable, "-c", "print('@@fase fine')"]
        lavoro.avvisa_fine = avvisi.append
        lavoro.FILE_CODA = self.d / "coda.json"
        lavoro.coordinamento.analisi_in_corso = lambda: []
        try:
            l = lavoro.Lavoro()
            film = []
            for nome in ("Rullo uno", "Rullo due", "Rullo tre"):
                video = self.d / f"{nome}.mov"
                video.write_text("finto")
                film.append(video)
                l.metti_in_coda(sessioni.nuova_sessione(nome, {"video": str(video)}))
            self.assertEqual([x["nome"] for x in l.elenco_coda()], ["Rullo uno", "Rullo due", "Rullo tre"])
            self.assertEqual(len(json.loads(lavoro.FILE_CODA.read_text())), 3, "la coda non è salvata")
            film[1].unlink()  # il secondo film non c'è più: la coda deve andare avanti lo stesso
            l.avvia_coda()
            limite = time.time() + 20
            while l.in_coda and time.time() < limite:
                time.sleep(0.05)
        finally:
            lavoro.comando, lavoro.avvisa_fine, lavoro.FILE_CODA, lavoro.coordinamento.analisi_in_corso = originali
        self.assertFalse(l.in_coda)
        self.assertEqual(l.coda, [])
        self.assertEqual(len(avvisi), 1, "a fine coda serve un solo avviso")
        e = l.esito
        self.assertTrue(e["coda"])
        self.assertEqual(e["stato"], "errore")
        self.assertEqual(len(e["punti"]), 3)
        self.assertIn("Rullo due: interrotta", e["punti"][1])
        self.assertTrue(e["punti"][2].startswith("Rullo tre"))

    def test_sessione_senza_film_non_entra_in_coda(self):
        l = lavoro.Lavoro()
        l.coda = []
        with self.assertRaises(RuntimeError):
            l.metti_in_coda(sessioni.nuova_sessione("Vuota"))


class SguardiComparse(unittest.TestCase):
    def test_soglie(self):
        comparsa, protagonista = [0.1, 0.1, 0.15, 0.17], [0.3, 0.1, 0.6, 0.5]
        self.assertEqual(rilevatori.soglia_sguardo(comparsa), rilevatori.SOGLIA_SGUARDO_COMPARSE)
        self.assertEqual(rilevatori.soglia_sguardo(protagonista), rilevatori.SOGLIA_SGUARDO)


class BancoDiProva(ConCartella):
    """Le segnalazioni rivedute diventano casi del banco; le statistiche dicono chi aveva ragione."""

    def test_revisioni_e_statistiche(self):
        lavoro = self.d / "lavoro_sessione"
        segn = segnalazioni_finte(lavoro, 3)
        dati = json.loads((lavoro / "segnalazioni.json").read_text())
        dati["segnalazioni"][1]["da_controllare"] = True
        dati["segnalazioni"][2]["da_controllare"] = True
        (lavoro / "segnalazioni.json").write_text(json.dumps(dati))
        app.rivedi(lavoro, segn[0]["id"], "confermato")
        app.rivedi(lavoro, segn[1]["id"], "confermato")   # da controllare, era un errore: ragione a Gemma
        app.rivedi(lavoro, segn[2]["id"], "falso")        # da controllare, era falso: ragione a Qwen3-VL
        s = banco.statistiche()
        self.assertEqual(s["casi"], 3)
        self.assertEqual(s["da_controllare"], {"gemma": 1, "qwen": 1})
        self.assertEqual(s["concordi"], {"giusti": 1, "sbagliati": 0})
        app.rivedi(lavoro, segn[2]["id"], "")             # revisione tolta: il caso esce dal banco
        self.assertEqual(banco.statistiche()["casi"], 2)
        shutil.rmtree(lavoro)                             # la sessione sparisce, il banco resta
        self.assertEqual(banco.statistiche()["casi"], 2)
        self.assertTrue(all((Path(c["cartella"]) / "dettaglio.jpg").exists() for c in banco.casi()))

    def test_riepilogo_della_prova(self):
        r = banco.riepilogo([
            {"categoria": "troupe", "vero": True, "verifica": True, "seconda": True, "esame": True},
            {"categoria": "troupe", "vero": True, "verifica": True, "seconda": False, "esame": False},
            {"categoria": "troupe", "vero": False, "verifica": False, "esame": False},
            {"categoria": "sguardi", "vero": True, "sguardo": True, "dubbio": False}])
        self.assertEqual(r["verifica_veri_segnalati"], {"giusti": 1, "totale": 2})
        self.assertEqual(r["verifica_veri_da_controllare"], {"giusti": 1, "totale": 2})
        self.assertEqual(r["verifica_falsi_scartati"], {"giusti": 1, "totale": 1})
        self.assertEqual(r["esame_veri_visti"], {"giusti": 1, "totale": 2})
        self.assertEqual(r["sguardi_veri_segnalati"], {"giusti": 1, "totale": 1})


class ZoomSulPetto(unittest.TestCase):
    def test_riquadri_riportati_al_fotogramma(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f000001.jpg"
            cv2.imwrite(str(f), np.zeros((500, 1000, 3), np.uint8))
            originali = rilevatori.petti, rilevatori.esamina
            rilevatori.petti = lambda img: [(100, 200, 300, 400)]   # un petto di 200×200 pixel
            rilevatori.esamina = lambda modello, immagine, categorie: [
                {"categoria": "lavalier", "voce": "lavalier", "descrizione": "", "riquadro": [.5, .5, 1, 1]}]
            try:
                trovati = rilevatori.esamina_zoom("m", f)
            finally:
                rilevatori.petti, rilevatori.esamina = originali
        self.assertEqual(trovati[0]["riquadro"], [0.2, 0.6, 0.3, 0.8])

    def test_solo_in_modalita_accurata(self):
        normale = murnau.Analisi(murnau.leggi_argomenti(["v.mov"]))
        accurata = murnau.Analisi(murnau.leggi_argomenti(["v.mov", "--accurato"]))
        senza_lavalier = murnau.Analisi(murnau.leggi_argomenti(["v.mov", "--accurato", "--categorie", "troupe"]))
        self.assertEqual((normale.zoom, accurata.zoom, senza_lavalier.zoom), (False, True, False))


class Report(ConCartella):
    def test_report_esportato_autonomo_e_leggero(self):
        import cv2
        import numpy as np
        segnalazioni_finte(self.d, 3)
        rumore = np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        cv2.imwrite(str(self.d / "segnalazioni" / "x.jpg"), rumore, [cv2.IMWRITE_JPEG_QUALITY, 88])
        dest = self.d / "esportato.html"
        dati, revisione = esporta.leggi(self.d)
        esporta.scrivi_report(self.d, dati, revisione, destinazione=dest)
        html = dest.read_text()
        self.assertNotIn('src="segnalazioni/', html)
        self.assertNotIn('href="data:', html, "il fotogramma è incorporato due volte")
        originale = (self.d / "segnalazioni" / "x.jpg").stat().st_size
        # Prima: tre immagini intere per segnalazione, in base64 (circa 12 volte l'originale per 3 segnalazioni).
        self.assertLess(len(html), 6 * originale, "le immagini incorporate non sono state ridotte")


class Fotogrammi(unittest.TestCase):
    def test_fotogramma_illeggibile(self):
        with tempfile.TemporaryDirectory() as d:
            rotto = Path(d) / "f000001.jpg"
            rotto.write_bytes(b"non sono un jpeg")
            with self.assertRaises(OSError):
                murnau.immagini_sospetto(rotto, [0.1, 0.1, 0.2, 0.2], "troupe")
            with self.assertRaises(OSError):
                rilevatori.Volti.frontali(type("V", (), {"cv2": __import__("cv2"), "np": None})(), rotto)

    def test_filtri_ffmpeg(self):
        self.assertEqual(murnau.filtri_ffmpeg("", "fps=1", "scale=1280:-2"), "fps=1,scale=1280:-2")
        self.assertEqual(murnau.filtri_ffmpeg("crop=1920:800:0:140", "scale=1280:-2"), "crop=1920:800:0:140,scale=1280:-2")


class StatoPerLInterfaccia(ConCartella):
    def test_versione_delle_segnalazioni(self):
        self.assertEqual(app.versione_segnalazioni(self.d), "")
        segnalazioni_finte(self.d, 2)
        prima = app.versione_segnalazioni(self.d)
        self.assertTrue(prima)
        self.assertEqual(app.versione_segnalazioni(self.d), prima)
        time.sleep(0.01)
        app.rivedi(self.d, "00000-troupe", "confermato")
        self.assertNotEqual(app.versione_segnalazioni(self.d), prima)

    def test_controlli_costosi_ricordati(self):
        chiamate = []
        for _ in range(3):
            app.recente("prova", 5, lambda: chiamate.append(1) or len(chiamate))
        self.assertEqual(len(chiamate), 1)


if __name__ == "__main__":
    unittest.main()
