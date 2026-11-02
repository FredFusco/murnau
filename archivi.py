"""Scrittura sicura dei file di Murnau.

- scrivi_json / scrivi_testo: il file nuovo si scrive accanto e poi prende il posto del
  vecchio in un colpo solo. Un'interruzione (Ferma, crash, Mac spento) lascia il file
  vecchio intero, mai uno scritto a metà.
- Archivio: risultati già calcolati in un file JSONL, una riga per risultato, per
  riprendere un'analisi interrotta dal punto in cui era arrivata.
"""

import json
import os
import tempfile
from pathlib import Path


def scrivi_testo(percorso: Path, testo: str) -> None:
    percorso = Path(percorso)
    fd, temporaneo = tempfile.mkstemp(dir=percorso.parent, prefix=f".{percorso.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(testo)
        os.replace(temporaneo, percorso)
    except BaseException:
        Path(temporaneo).unlink(missing_ok=True)
        raise


def scrivi_json(percorso: Path, dati, indent: int = 1) -> None:
    scrivi_testo(percorso, json.dumps(dati, ensure_ascii=False, indent=indent))


class Archivio:
    """{chiave: risultato} letto da un file JSONL e aggiornato una riga alla volta."""

    def __init__(self, percorso: Path):
        self.percorso = Path(percorso)
        self.dati = {}
        if not self.percorso.exists():
            return
        contenuto = self.percorso.read_bytes()
        fine = contenuto.rfind(b"\n") + 1
        if fine < len(contenuto):
            # L'ultima riga è rimasta a metà (analisi interrotta mentre scriveva): si toglie, altrimenti
            # la prossima riga le verrebbe attaccata e andrebbe persa anche quella.
            with self.percorso.open("r+b") as f:
                f.truncate(fine)
        for riga in contenuto[:fine].decode("utf-8", errors="replace").splitlines():
            try:
                r = json.loads(riga)
                self.dati[r["k"]] = r["v"]
            except (ValueError, KeyError, TypeError):
                pass

    def __contains__(self, k):
        return k in self.dati

    def __getitem__(self, k):
        return self.dati[k]

    def get(self, k, predefinito=None):
        return self.dati.get(k, predefinito)

    def metti(self, k: str, v) -> None:
        self.dati[k] = v
        with self.percorso.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")
