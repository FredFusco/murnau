# Murnau

Cerca gli errori di ripresa in un film montato e li segnala con timecode, fotogramma e dettaglio ingrandito:

- **Troupe o attrezzatura in campo**: boom, macchina da presa, cavalletti, stativi, luci da set, operatori
- **Troupe nei riflessi**: specchi, vetri, finestre, occhiali, carrozzerie, superfici lucide
- **Sguardi in macchina** di attori e comparse
- **Microfoni lavalier** visibili sugli attori
- **Prese elettriche e cavi** a terra o sulle pareti

Il film non esce dal computer. Gli unici collegamenti a internet servono a scaricare i modelli la prima volta e a controllare se ne esistono versioni nuove. Il controllo legge soltanto il numero di versione pubblicato e non invia nulla del film.

## Come si usa

1. Apri **Murnau.app**.
2. Trascina nella finestra lo scarico del film (`.mov`, `.mxf`, `.mp4`) e, se puoi, l'**EDL** o l'**AAF** della sequenza, anche insieme o in una cartella: Murnau li riconosce e li mette nei riquadri **Film** e **Montaggio**, con durata, fps, risoluzione e timecode o numero di eventi. Con il montaggio i tagli sono esatti e ogni segnalazione riporta la clip sorgente (con l'AAF anche scena, inquadratura e ciak dal bin). Se un file finisce nel riquadro sbagliato, usa **Usa come…** o **Togli**.
3. Scegli cosa cercare e la precisione, poi premi **Analizza il film**.
4. A fine lavoro, insieme al suono, compare un **riquadro con l'esito**: tutto a posto, cose da verificare (con l'elenco) o un errore che ha interrotto l'analisi (con il motivo in parole semplici; il testo tecnico resta in *Dettagli dell'elaborazione*). Resta finché non premi **OK**, anche se ricarichi la pagina.
5. Nella scheda **Segnalazioni** rivedi ogni caso con **Errore confermato** o **Falso allarme**. Se vuoi, aggiungi una nota.
6. Nel pannello **Esporta** (anche *File › Esporta…*, ⌘E) scegli i file che ti servono e la cartella, poi premi **Esporta**. I file prendono il nome della sessione: `<sessione> - marker Avid.txt`, `<sessione> - marker Resolve.edl`, `<sessione> - segnalazioni.csv`, `<sessione> - report.html`. I falsi allarmi sono esclusi. Murnau ricorda le scelte; dopo aver rivisto altre segnalazioni puoi riesportare.
7. In Media Composer apri la finestra *Markers*, poi nel menu della finestra scegli *Import Markers* e il file `… - marker Avid.txt`: i marker compaiono sulla sequenza al timecode di ogni errore, colorati per categoria.

Se premi **Ferma** o chiudi l'app non è un errore: niente riquadro e niente suono. Rilanciando, l'analisi riprende dal punto in cui si era fermata.

### Colori dei marker

| Categoria | Colore |
|---|---|
| Troupe o attrezzatura in campo | rosso |
| Troupe nei riflessi | magenta |
| Sguardo in macchina | giallo |
| Microfono lavalier | ciano |
| Prese e cavi | blu |

## Sessioni

Ogni lavoro è una **sessione**: film, montaggio, opzioni, cartella di lavoro e cartella in cui esportare, salvati da soli a ogni modifica. In alto a destra:

- **Nuova** apre una sessione vuota;
- **Apri…** elenca le sessioni (film, numero di segnalazioni, data) e permette di riaprirle o di **eliminarle**. Eliminando una sessione puoi scegliere di spostare nel Cestino anche la sua cartella di lavoro e i file esportati; il film e il montaggio originali non vengono mai toccati;
- **Salva** conferma il nome scritto nel campo; anche la cartella di lavoro della sessione prende il nuovo nome.

Le stesse voci sono nel menu **File** (⌘N, ⌘O, ⌘S).

## Libera spazio

**Libera spazio…** (in alto, o nel menu Aiuto) mostra quanto occupano i file creati da Murnau e lascia scegliere cosa cancellare: fotogrammi estratti (si ricreano se rilanci un'analisi), versioni precedenti dell'app, registro, file di installazione delle librerie, modelli di prova non usati e altri modelli di Ollama. Segnalazioni, revisioni e sessioni non vengono toccate.

I modelli di Murnau stanno nella sua cartella (`~/Library/Application Support/Murnau/modelli`), così la pulizia di altre app, come Vertov, non li cancella per errore.

## Insieme a Vertov e Ozu

Le tre app usano lo stesso modello, Qwen3-VL, ma con impostazioni diverse: se lavorano insieme, Ollama deve ricaricarlo (20 GB) a ogni cambio e tutto rallenta di molto. Per questo:

- **Precedenza a Vertov**: prima di usare il modello Murnau controlla se un'analisi di Vertov è in corso e, se sì, aspetta («In attesa: Vertov sta usando il modello») e riparte da sola. Ozu a sua volta aspetta Murnau.
- **Una sola analisi di Murnau alla volta**: l'app non ne avvia una se ce n'è già un'altra in corso.
- **Memoria**: Murnau tiene il modello caricato solo 5 minuti dopo l'ultima richiesta e, a fine analisi, lo toglie subito dalla memoria, se è la copia caricata da lei e nessun'altra app lo sta usando.
- **Chiusura vera**: chiudendo l'app l'analisi si ferma, insieme all'estrazione dei fotogrammi. Se l'app si chiude in modo anomalo (crash, chiusura forzata), l'analisi se ne accorge e si spegne da sola entro un secondo. Si può riprendere da dove era arrivata.
- **Niente analisi nascoste**: anche un'analisi lanciata da Terminale si vede nell'app, in cima alla scheda Analisi, con il film, da quanto tempo gira, la fase e un pulsante **Ferma**. All'avvio e alla fine manda una notifica di macOS.

## Come funziona

1. Estrae un fotogramma al secondo (senza le bande nere) e scarta quelli a ridosso dei tagli.
2. **OWLv2** cerca in ogni fotogramma microfoni, macchine da presa, stativi, prese e cavi. È veloce ma impreciso: serve solo a trovare i sospetti.
3. **YuNet** trova i volti rivolti verso l'obiettivo, candidati allo sguardo in macchina.
4. **Qwen3-VL** esamina da 1 a 4 fotogrammi per inquadratura (uno ogni 2 secondi in modalità accurata) con domande precise. In questo passaggio trova anche i riflessi.
5. Ogni sospetto viene **verificato** di nuovo da Qwen3-VL sul fotogramma con il riquadro e sul ritaglio ingrandito. Diventa una segnalazione solo se supera la verifica.

### Tempi

Su M5 Max servono circa 0,5 secondi a fotogramma per oggetti e volti e circa 8 secondi per ogni esame di Qwen3-VL. Per un lungometraggio di 2 ore con 1.500 inquadrature sono 3–5 ore in modalità normale: conviene lanciarlo di notte.

## File di lavoro ed esportazioni

Murnau non scrive niente nella tua cartella finché non esporti. I file con cui lavora stanno dentro la cartella di Murnau, in `Murnau – file di lavoro/`, una cartella per sessione con lo stesso nome della sessione. La vedi con **Mostra** in Materiali o *File › Mostra i file di lavoro*. Git, il pacchetto degli aggiornamenti e l'installazione degli aggiornamenti la ignorano, quindi non finisce mai su GitHub e un aggiornamento non la tocca; se sposti o rinomini la cartella di Murnau, le sessioni ritrovano i loro file:

- `segnalazioni.json` e `revisione.json`: le segnalazioni e le tue revisioni
- `segnalazioni/`: fotogrammi e dettagli delle segnalazioni
- `report_murnau.html`, `marker_avid.txt`, `marker_resolve.edl`, `segnalazioni.csv`: le versioni di lavoro dei file che puoi esportare
- `lavoro/`: fotogrammi e calcoli intermedi (circa 150 MB ogni 10 minuti di film; *Libera spazio…* può cancellare i fotogrammi, che si ricreano se servono)

Nella cartella in cui esporti (all'inizio quella del film) finiscono solo i file scelti in **Esporta**. Il report esportato contiene anche le immagini, quindi si apre e si invia da solo.

All'avvio Murnau porta dentro la propria cartella le cartelle di lavoro create altrove dalle versioni precedenti (in `My Drive/CLAUDE` o in Application Support), e sposta nella cartella di lavoro i file che le versioni ancora più vecchie lasciavano nella cartella dei risultati: li sposta, non li cancella, e non tocca mai il film, il montaggio o i file di Vertov (anche il suo `marker_resolve.edl`, che inizia con «TITLE: Descrizioni inquadrature» invece che con «TITLE: Murnau»).

## Aggiornamenti

All'avvio, al massimo una volta al giorno, Murnau controlla se ci sono versioni nuove di:

- **Murnau** stesso (quando gli aggiornamenti saranno pubblicati online: l'indirizzo va in `aggiornamenti.json`);
- **ffmpeg** e **Ollama** (con Homebrew);
- le **librerie Python** (condivise con Vertov: dopo l'aggiornamento si verifica che funzionino per entrambe le app, altrimenti si torna alle versioni di prima);
- i modelli **Qwen3-VL** e **OWLv2**.

Se ne trova, mostra l'elenco e chiede cosa installare: **Aggiorna**, **Più tardi** o **Salta questa versione**. Il controllo automatico si può spegnere (**Controlla all'avvio**) e si può sempre lanciare a mano con **Aggiornamenti…** o dal menu Aiuto. Durante un'analisi non si aggiorna. I file sostituiti dagli aggiornamenti dell'app vengono conservati, per poter tornare indietro.

## Pubblicare una nuova versione

Il codice è su GitHub in [FredFusco/murnau](https://github.com/FredFusco/murnau) (pubblico: solo il programma, mai film o risultati, grazie a `.gitignore`). Per pubblicare una versione, dopo il commit:

```bash
~/.venvs/descrittore/bin/python prepara_aggiornamento.py 1.1.0 "Novità uno" "Novità due" --pubblica
```

Crea il pacchetto e l'annuncio in `dist/` e li carica come release: le copie di Murnau li vedono al controllo successivo, con le novità elencate.

## Da riga di comando

```bash
~/.venvs/descrittore/bin/python murnau.py FILM.mov --montaggio FILM.aaf --out cartella_di_lavoro
```

Da riga di comando `--out` è la cartella di lavoro: lì finiscono tutti i file elencati sopra. A fine lavoro l'analisi segnala le cose da verificare con righe `@@avviso` (vedi `avviso()` in `murnau.py`); l'app le mostra nel riquadro dell'esito.

| Parametro | Cosa fa |
|---|---|
| `--montaggio` | EDL CMX3600 o AAF della sequenza |
| `--categorie` | ad esempio `troupe,riflessi,sguardi` (predefinito: tutte) |
| `--accurato` | Qwen3-VL esamina un fotogramma ogni 2 secondi |
| `--passo` | secondi tra un fotogramma e l'altro per oggetti e volti (predefinito 1) |
| `--tc-inizio` | timecode del primo fotogramma, se il file non lo dichiara |
| `--da N --a M` | analizza solo le inquadrature da N a M |

## Requisiti

Murnau usa lo stesso ambiente Python e lo stesso Ollama già installati sul Mac (`~/.venvs/descrittore`, modello `qwen3-vl:30b-a3b-instruct`), ma è un'app a sé: ha dati, impostazioni e finestra propri, in `~/Library/Application Support/Murnau`.

## Limiti

- Un riflesso minuscolo o un boom che entra per pochi fotogrammi tra due campioni può sfuggire: la modalità accurata riduce il rischio.
- Prese e cavi sono spesso arredo voluto: Murnau li segnala tutti, sta a te decidere.
- Uno sguardo in macchina voluto (una soggettiva, un a parte) viene segnalato come gli altri.
