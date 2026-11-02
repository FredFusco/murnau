"""Test di Murnau che non usano modelli né Ollama: girano in pochi secondi.

    ~/.venvs/descrittore/bin/python -m unittest discover -s tests -v
"""

import json
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

    def tearDown(self):
        for k, v in self._originali.items():
            setattr(sessioni, k, v)
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
        self.assertEqual(len(json.loads((self.d / "revisione.json").read_text())), 40)

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
