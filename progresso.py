"""Come l'analisi (murnau.py) racconta all'app a che punto è.

Righe sullo standard output che iniziano con "@@" e che l'app legge (lavoro.py):
  @@fase NOME                    inizia una fase
  @@avanz NOME FATTI TOTALE      avanzamento della fase
  @@piano NOME=UNITÀ:SECONDI ... quanto lavoro resta in ogni fase e quanto costa un'unità
  @@attesa TESTO                 l'analisi aspetta (testo vuoto: riparte)
  @@avviso TESTO                 una cosa da verificare, elencata a fine lavoro
Le stesse informazioni vanno anche nel registro delle analisi in corso, se c'è.
"""

# Secondi stimati per unità di lavoro di ogni fase (M5 Max, Ollama libero): servono a pesare le
# fasi nella percentuale complessiva la prima volta; poi l'app usa i tempi misurati nelle analisi
# precedenti su questo Mac.
COSTI = {"fotogrammi": .04, "oggetti": .12, "volti": .08, "modello": 45.0, "esame": 4.5, "verifica": 1.5}

registro = None  # coordinamento.Registro dell'analisi in corso


def fase(nome: str) -> None:
    print(f"@@fase {nome}", flush=True)
    if registro:
        registro.aggiorna(fase=nome, fatti=0, totale=0)


def avanzamento(nome: str, fatti: int, totale: int) -> None:
    # Al più circa 200 righe per fase: l'app non ha bisogno di più dettaglio.
    if totale <= 200 or fatti % max(1, totale // 200) == 0 or fatti == totale:
        print(f"@@avanz {nome} {fatti} {totale}", flush=True)
    if registro:
        registro.aggiorna(fatti=fatti, totale=totale)


def piano(**unita) -> None:
    """Annuncia quanto lavoro resta in ogni fase, nell'ordine in cui verrà fatto."""
    print("@@piano " + " ".join(f"{nome}={n}:{COSTI[nome]}" for nome, n in unita.items()), flush=True)


def attesa(testo: str) -> None:
    print(f"@@attesa {testo}".rstrip(), flush=True)
    if registro:
        registro.aggiorna(attesa=testo)


def avviso(testo: str) -> None:
    print(f"@@avviso {testo}", flush=True)


def elenco(numeri: list, massimo: int = 20) -> str:
    """"3, 7, 12…": i primi numeri di un elenco lungo, per gli avvisi."""
    numeri = sorted(numeri)
    return ", ".join(map(str, numeri[:massimo])) + ("…" if len(numeri) > massimo else "")


def quanti(n: int, uno: str, molti: str) -> str:
    """"1 fotogramma non è stato esaminato" o "3 fotogrammi non sono stati esaminati"."""
    return f"{n} {uno if n == 1 else molti}"
