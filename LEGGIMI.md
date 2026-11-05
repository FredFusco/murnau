# Murnau

Cerca gli errori di ripresa in un film montato e li segnala con timecode, fotogramma e dettaglio ingrandito:

- **Troupe o attrezzatura in campo**: boom, macchina da presa, cavalletti, stativi, luci da set, operatori
- **Troupe nei riflessi**: specchi, vetri, finestre, occhiali, carrozzerie, superfici lucide
- **Sguardi in macchina**: soprattutto le comparse sullo sfondo che guardano verso l'obiettivo; i volti in primo piano solo come «da controllare»
- **Microfoni lavalier** visibili sugli attori
- **Cavi della troupe**: cavi di luci, macchina da presa o audio a terra, prolunghe, nastro da set (non le prese a muro né i cavi degli oggetti di scena, che sono arredo)

Il film non esce dal computer. Gli unici collegamenti a internet servono a scaricare i modelli la prima volta e a controllare se ne esistono versioni nuove. Il controllo legge soltanto il numero di versione pubblicato e non invia nulla del film.

## L'app

Murnau è un'app del Mac: sta nella cartella Applicazioni dell'utente (`~/Applications/Murnau.app`), la trovi con Spotlight e Launchpad e la puoi tenere nel Dock. Questa cartella contiene solo il codice sorgente, da cui si costruisce e si installa l'app con `mac/compila.sh --installa`.

**Murnau.app è un'app nativa per Mac** (Swift, sorgente in `mac/Murnau.swift`, si compila con `mac/compila.sh`; bastano gli strumenti da riga di comando di Xcode). Si apre subito con il logo e «Avvio di Murnau…», ha menu e scorciatoie native (con *Murnau › Impostazioni…* ⌘,), finestre di scelta dei file del Mac, la percentuale dell'analisi sull'icona nel Dock, la conferma dell'uscita con il logo di Murnau, ricorda posizione e misura della finestra e riceve film, montaggi e cartelle trascinati sulla finestra o sull'icona nel Dock, o aperti dal Finder con *Apri con › Murnau* (Murnau non diventa l'app predefinita per quei file). Il motore resta in Python: l'app avvia `app.py --servizio`, cioè solo il server dell'interfaccia, e lo ferma all'uscita (nessun processo resta acceso); se il motore si ferma da solo, l'app propone di riavviarlo. Dopo un aggiornamento il server esce con il codice 75 e l'app si chiude e si riapre senza chiedere. `python app.py` senza `--servizio` apre ancora la finestra di pywebview, come prima: resta come ripiego e per il Terminale.

**Murnau.app è completa:** dentro, in `Contents/Resources/programma`, c'è tutto il programma (i file che `.gitignore` ammette); fuori restano solo l'ambiente Python (`~/.venvs/descrittore`, condiviso con Vertov), i dati (`~/Library/Application Support/Murnau`, con sessioni, file di lavoro e banco di prova), le cache dei modelli e Ollama. `mac/compila.sh` la costruisce in `~/Library/Caches/Murnau/` (fuori da Drive, perché Spotlight non la trovi due volte) e con `--installa` la mette in `~/Applications`, scambiandola con quella che c'è. Il motore gira con `PYTHONDONTWRITEBYTECODE=1`, così non scrive file dentro l'app e la firma resta valida. Al primo avvio su un Mac nuovo Murnau prepara da sé l'ambiente Python (PyTorch, OpenCV, Transformers, pywebview, pyaaf2): ci vuole qualche minuto.

L'app e il motore scrivono nello stesso registro, `~/Library/Application Support/Murnau/murnau.log` (*Aiuto › Apri il registro di Murnau*).

## Come si usa

1. Apri **Murnau** (dalla cartella Applicazioni, da Spotlight o dal Dock).
2. Trascina nella finestra lo scarico del film (`.mov`, `.mxf`, `.mp4`) e, se puoi, l'**EDL** o l'**AAF** della sequenza, anche insieme o in una cartella: Murnau li riconosce e li mette nei riquadri **Film** e **Montaggio**, con durata, fps, risoluzione e timecode o numero di eventi. Con il montaggio i tagli sono esatti e ogni segnalazione riporta la clip sorgente (con l'AAF anche scena, inquadratura e ciak dal bin). Se un file finisce nel riquadro sbagliato, usa **Usa come…** o **Togli**.
3. Scegli cosa cercare e la precisione, poi premi **Analizza il film**.
4. A fine lavoro, insieme al suono, compare un **riquadro con l'esito**: tutto a posto, cose da verificare (con l'elenco) o un errore che ha interrotto l'analisi (con il motivo in parole semplici; il testo tecnico resta in *Dettagli dell'elaborazione*). Resta finché non premi **OK**, anche se ricarichi la pagina.
5. Nella scheda **Segnalazioni** rivedi ogni caso con **Errore confermato** o **Falso allarme**. Se vuoi, aggiungi una nota.
6. Nella scheda **Esporta**, la terza (⌘3, o *File › Esporta…*, ⌘E), vedi quante segnalazioni finiranno nei file; scegli i file che ti servono e la cartella, poi premi **Esporta**. I file prendono il nome della sessione: `<sessione> - marker Avid.txt`, `<sessione> - marker Resolve.edl`, `<sessione> - segnalazioni.csv`, `<sessione> - report.html`. I falsi allarmi sono esclusi. Murnau ricorda le scelte; dopo aver rivisto altre segnalazioni puoi riesportare.
7. In Media Composer apri la finestra *Markers*, poi nel menu della finestra scegli *Import Markers* e il file `… - marker Avid.txt`: i marker compaiono sulla sequenza al timecode di ogni errore, colorati per categoria.

Durante l'analisi il Mac non va in stop (lo schermo invece può spegnersi), anche se l'hai lanciata da Terminale; a batteria però la GPU rallenta e l'analisi dura circa il doppio: la scheda Analisi te lo ricorda.

Nella scheda **Segnalazioni** puoi rivedere con la tastiera: <kbd>↑</kbd> <kbd>↓</kbd> (o <kbd>K</kbd> <kbd>J</kbd>) per scorrere, <kbd>C</kbd> errore confermato, <kbd>F</kbd> falso allarme (e si passa alla prossima), <kbd>N</kbd> per scrivere una nota, <kbd>Esc</kbd> per uscirne.

### Coda per la notte

Per analizzare più sessioni una dopo l'altra (per esempio i rulli di un film): apri ogni sessione e premi **Metti in coda** (anche nel menu *Analisi*), poi **Avvia la coda**. La coda si vede nella scheda Analisi, con la sessione in corso e quelle in attesa; ✕ toglie una sessione. Una sessione che non parte (per esempio perché il film è stato spostato) non ferma le altre. Alla fine un solo suono e un solo riquadro riassumono com'è andata ciascuna; le segnalazioni le trovi aprendo le sessioni. **Ferma** ferma anche la coda: le sessioni non ancora analizzate restano in coda.

Chiudendo Murnau compare un riquadro di conferma con **Non mostrare più**: spuntato, le volte successive l'app si chiude subito, tranne quando un'analisi è in corso. *Murnau › Impostazioni…* (⌘,) lo riattiva.

Se premi **Ferma** o chiudi l'app non è un errore: niente riquadro e niente suono. Rilanciando, l'analisi riprende dal punto in cui si era fermata.

### Colori dei marker

| Categoria | Colore |
|---|---|
| Troupe o attrezzatura in campo | rosso |
| Troupe nei riflessi | magenta |
| Sguardo in macchina | giallo |
| Microfono lavalier | ciano |
| Cavi della troupe | blu |

## Banco di prova

Ogni segnalazione che segni come **Errore confermato** o **Falso allarme** diventa un caso del banco di prova: le sue immagini (fotogramma con il riquadro, dettaglio e, se si ritrova negli archivi dell'analisi, il fotogramma pulito) e il tuo giudizio. Il banco sta in `~/Library/Application Support/Murnau/banco`, sul Mac e mai su GitHub, e resta anche se elimini la sessione; *Libera spazio* non lo tocca. Se togli il giudizio, il caso esce dal banco.

*Aiuto › Banco di prova…* mostra quante segnalazioni hai rivisto per categoria e, per quelle *da controllare*, quante volte aveva ragione Gemma 4 (era un errore) e quante Qwen3-VL (era un falso allarme), più il risultato dell'ultima prova dei modelli.

La prova rifà su tutti i casi la verifica a due livelli, l'esame del fotogramma pulito e la misura dello sguardo, e confronta con i tuoi giudizi. Si lancia da Terminale, anche con modelli diversi, prima di cambiare un modello o le domande:

```bash
~/.venvs/descrittore/bin/python banco.py prova
~/.venvs/descrittore/bin/python banco.py prova --seconda qwen3.8:27b-dflash2-nvfp4
```

Il risultato resta in `banco/prove/` e l'ultimo compare nella finestra del banco. Come le analisi, la prova dà la precedenza a Vertov.

## Sessioni

Ogni lavoro è una **sessione**: film, montaggio, opzioni, cartella di lavoro e cartella in cui esportare, salvati da soli a ogni modifica. In alto a destra:

- **Nuova** apre una sessione vuota;
- **Apri…** elenca le sessioni (film, numero di segnalazioni, data) e permette di riaprirle o di **eliminarle**. Eliminando una sessione puoi scegliere di spostare nel Cestino anche la sua cartella di lavoro e i file esportati; il film e il montaggio originali non vengono mai toccati;
- **Salva** conferma il nome scritto nel campo; anche la cartella di lavoro della sessione prende il nuovo nome.

Le stesse voci sono nel menu **File** (⌘N, ⌘O, ⌘S).

## Libera spazio

**Libera spazio…** (in alto, o nel menu Aiuto) mostra quanto occupano i file creati da Murnau e lascia scegliere cosa cancellare: fotogrammi estratti (si ricreano se rilanci un'analisi), versioni precedenti dell'app, registro, file di installazione delle librerie, modelli di prova non usati e altri modelli di Ollama. Segnalazioni, revisioni e sessioni non vengono toccate. Non propone mai i modelli usati da Murnau, Vertov e Ozu: quelli delle altre app li legge dal loro codice.

I modelli di Murnau stanno nella sua cartella (`~/Library/Application Support/Murnau/modelli`), così la pulizia di altre app, come Vertov, non li cancella per errore.

## Insieme a Vertov e Ozu

Murnau usa due modelli: **Qwen3-VL**, solo suo, e **Gemma 4 26B**, lo stesso di Vertov e Ozu, con impostazioni diverse dalle loro: se lavorano insieme, Ollama deve ricaricarli (circa 20 GB) a ogni cambio e tutto rallenta di molto. Per questo:

- **Precedenza a Vertov**: prima di usare il modello Murnau controlla se un'analisi di Vertov è in corso e, se sì, aspetta («In attesa: Vertov sta usando il modello») e riparte da sola. Ozu a sua volta aspetta Murnau.
- **Una sola analisi di Murnau alla volta**: l'app non ne avvia una se ce n'è già un'altra in corso.
- **Memoria**: Murnau tiene il modello caricato solo 5 minuti dopo l'ultima richiesta e, a fine analisi, lo toglie subito dalla memoria, se è la copia caricata da lei e nessun'altra app lo sta usando.
- **Chiusura vera**: chiudendo l'app l'analisi si ferma, insieme all'estrazione dei fotogrammi. Se l'app si chiude in modo anomalo (crash, chiusura forzata), l'analisi se ne accorge e si spegne da sola entro un secondo. Si può riprendere da dove era arrivata.
- **Niente analisi nascoste**: anche un'analisi lanciata da Terminale si vede nell'app, in cima alla scheda Analisi, con il film, da quanto tempo gira, la fase e un pulsante **Ferma**. All'avvio e alla fine manda una notifica di macOS.

## Come funziona

1. Estrae un fotogramma al secondo (senza le bande nere) e scarta quelli a ridosso dei tagli.
2. **OWLv2** cerca in ogni fotogramma microfoni, macchine da presa, stativi, cavi e prolunghe. È veloce ma impreciso: serve solo a trovare i sospetti. Sulla GPU del Mac lavora a mezza precisione (circa tre volte più veloce, stessi sospetti) e alla fine restituisce la memoria, che serve a Qwen3-VL.
3. **YuNet** trova i volti rivolti verso l'obiettivo e un modello dello sguardo (L2CS) misura di quanti gradi lo sguardo si discosta dall'obiettivo, come mediana su sette ritagli appena diversi del volto: la misura singola oscilla di qualche grado anche per pochi pixel. Le **comparse** (volti piccoli) si segnalano fino a 10°: lì uno sguardo verso l'obiettivo è quasi sempre un errore. I **volti in primo piano** solo entro 2° e sempre come «da controllare»: sui casi rivisti nessuna soglia, e nessuno dei 12 modelli provati, distingueva chi guarda nell'obiettivo dal protagonista che guarda il partner appena accanto alla macchina.
4. **Qwen3-VL** esamina da 1 a 4 fotogrammi per inquadratura (uno ogni 2 secondi in modalità accurata) con domande precise. In questo passaggio trova anche i riflessi. In **modalità accurata** esamina anche il **petto ingrandito** di ogni personaggio, solo per i lavalier: nel fotogramma intero un lavalier è di pochi pixel, e sul banco di prova l'esame normale ne vedeva 1 su 8, con lo zoom 4 su 8. Costa circa il doppio del tempo dell'esame, per questo c'è solo nella modalità accurata. Lo zoom sul pavimento (cavi) e sul bordo alto (boom) è stato provato e scartato: trovava pochissimo in più e molti falsi allarmi.
5. Ogni sospetto viene **verificato** da **Gemma 4** sul fotogramma con il riquadro e sul ritaglio ingrandito, scegliendo tra opzioni precise (per esempio «microfono tenuto in mano da un personaggio (di scena)» o «microfono boom»): è il programma, non il modello, a decidere quali opzioni sono errori.
6. Ciò che Gemma 4 accetta passa dalla **seconda opinione** di Qwen3-VL. Se i due sono d'accordo è una segnalazione normale; se no resta, ma **da controllare** (certezza bassa, con la seconda opinione scritta sotto) e la trovi con il filtro *Da controllare*. Gemma è veloce ma generoso, Qwen prudente: così non sparisce niente in silenzio, e le tue revisioni diranno quale dei due ha più spesso ragione. Nei marker queste segnalazioni iniziano con «[da controllare]».

I fotogrammi si analizzano senza le bande nere, anche quando dentro le bande ci sono scritte sovraimpresse (timecode, nome del rullo).

### Perché questi modelli

A novembre 2026 sono stati confrontati 12 modelli gratuiti e locali (Qwen3-VL, Gemma 4 26B e 31B, Qwen 3.5 e 3.8, Nemotron 3.5 Bolt, Bonsai-2, LFM3-VL e i modelli «giudice» pplx-decider, clef-flash e d3) sulle segnalazioni già rivedute. Qwen3-VL è l'unico che nell'esame trova i dettagli piccoli, come un riflesso di luce da set. Gemma 4 26B sui casi già rivisti verificava con la stessa precisione ed era 2,6 volte più veloce, ma sull'intero rullo è risultato troppo generoso (collane e pieghe scambiate per lavalier), mentre Qwen3-VL da solo scartava quasi tutto: da qui i due livelli. Gemma 4 gira con il motore MLX di Ollama, che non accetta la «risposta a schema»: Murnau allora descrive lo schema nel testo e controlla la risposta; un'opzione scritta male diventa sempre la più prudente.

### Tempi

Su M5 Max servono circa 0,05 secondi a fotogramma per gli oggetti, 0,08 per i volti, circa 3–4 secondi per ogni esame di Qwen3-VL e poco più di 1 secondo per ogni verifica di Gemma 4: l'esame occupa la maggior parte del tempo. Per un lungometraggio di 2 ore con 1.500 inquadrature sono 3–5 ore in modalità normale: conviene lanciarlo di notte.

### Se qualcosa va storto

- Ogni risultato viene salvato appena calcolato: un'analisi interrotta (Ferma, crash, Mac spento) riprende da dove era arrivata, anche se l'interruzione ha lasciato un risultato scritto a metà.
- Sessioni, segnalazioni e revisioni si scrivono in un colpo solo: un'interruzione lascia la versione precedente intera, mai un file a metà. Se per un istante la sessione aperta non si legge, Murnau riprova; se proprio non si legge apre la più recente, senza crearne una vuota.
- Se Ollama si chiude durante l'analisi, Murnau lo riavvia e lo aspetta fino a due minuti prima di rinunciare a quel fotogramma.
- Un fotogramma che non si legge viene saltato e indicato nel riquadro dell'esito, invece di fermare l'analisi.

## File di lavoro ed esportazioni

Murnau non scrive niente nella tua cartella finché non esporti. I file con cui lavora stanno con gli altri dati di Murnau, in `~/Library/Application Support/Murnau/file di lavoro/`, una cartella per sessione con lo stesso nome della sessione. La vedi con **Mostra** in Materiali o *File › Mostra i file di lavoro*. Non stanno mai dentro Murnau.app (che non cambia mentre gira) né nella cartella del codice, quindi non finiscono su GitHub e un aggiornamento non li tocca:

- `segnalazioni.json` e `revisione.json`: le segnalazioni e le tue revisioni
- `segnalazioni/`: fotogrammi e dettagli delle segnalazioni
- `report_murnau.html`, `marker_avid.txt`, `marker_resolve.edl`, `segnalazioni.csv`: le versioni di lavoro dei file che puoi esportare
- `lavoro/`: fotogrammi e calcoli intermedi (circa 150 MB ogni 10 minuti di film; *Libera spazio…* può cancellare i fotogrammi, che si ricreano se servono)

Nella cartella in cui esporti (all'inizio quella del film) finiscono solo i file scelti in **Esporta**. Il report esportato contiene anche le immagini, quindi si apre e si invia da solo.

All'avvio Murnau porta nella cartella dei file di lavoro le cartelle create altrove dalle versioni precedenti (fino alla 1.0.9 stavano nella cartella del codice, in `Murnau – file di lavoro/`), e sposta nella cartella di lavoro i file che le versioni ancora più vecchie lasciavano nella cartella dei risultati: li sposta, non li cancella, e non tocca mai il film, il montaggio o i file di Vertov (anche il suo `marker_resolve.edl`, che inizia con «TITLE: Descrizioni inquadrature» invece che con «TITLE: Murnau»).

## Aggiornamenti

All'avvio, al massimo una volta al giorno, Murnau controlla se ci sono versioni nuove di:

- **Murnau** stesso (quando gli aggiornamenti saranno pubblicati online: l'indirizzo va in `aggiornamenti.json`);
- **ffmpeg** e **Ollama** (con Homebrew);
- le **librerie Python** (condivise con Vertov: dopo l'aggiornamento si verifica che funzionino per entrambe le app, altrimenti si torna alle versioni di prima);
- i modelli **Qwen3-VL**, **Gemma 4** e **OWLv2**.

Se ne trova, mostra l'elenco e chiede cosa installare: **Aggiorna**, **Più tardi** o **Salta questa versione**. Il controllo automatico si può spegnere (**Controlla all'avvio**, anche in *Impostazioni*) e si può sempre lanciare a mano con **Aggiornamenti…** o dal menu Aiuto. Durante un'analisi non si aggiorna.

Un aggiornamento di Murnau scarica Murnau.app nuova completa, la prepara accanto a quella installata e le scambia: il Murnau aperto continua a girare fino al riavvio, che avviene da solo. La versione di prima va in `~/Library/Application Support/Murnau/versioni_precedenti/`, per poter tornare indietro. Dal codice sorgente gli aggiornamenti di Murnau non si installano: lì si usano git e `mac/compila.sh --installa`.

## Pubblicare una nuova versione

Il codice è su GitHub in [FredFusco/murnau](https://github.com/FredFusco/murnau) (pubblico: solo il programma, mai film o risultati, grazie a `.gitignore`). Per pubblicare una versione, dopo il commit:

```bash
~/.venvs/descrittore/bin/python prepara_aggiornamento.py 1.1.0 "Novità uno" "Novità due" --pubblica
```

Scrive VERSIONE, costruisce Murnau.app con `mac/compila.sh`, crea in `dist/` il pacchetto (lo zip con dentro l'app completa) e l'annuncio e li carica come release: le copie di Murnau li vedono al controllo successivo, con le novità elencate. Per avere la versione nuova anche su questo Mac: `mac/compila.sh --installa`.

## Da riga di comando

```bash
~/.venvs/descrittore/bin/python murnau.py FILM.mov --montaggio FILM.aaf --out cartella_di_lavoro
```

Da riga di comando `--out` è la cartella di lavoro: lì finiscono tutti i file elencati sopra. L'analisi racconta all'app a che punto è con righe che iniziano con `@@` (fase, avanzamento, piano del lavoro, attese, cose da verificare): sono descritte in `progresso.py`.

| Parametro | Cosa fa |
|---|---|
| `--montaggio` | EDL CMX3600 o AAF della sequenza |
| `--categorie` | ad esempio `troupe,riflessi,sguardi` (predefinito: tutte) |
| `--accurato` | Qwen3-VL esamina un fotogramma ogni 2 secondi e il petto ingrandito dei personaggi (lavalier) |
| `--modello`, `--modello-verifica` | i modelli di Ollama per l'esame e per la verifica |
| `--passo` | secondi tra un fotogramma e l'altro per oggetti e volti (predefinito 1) |
| `--tc-inizio` | timecode del primo fotogramma, se il file non lo dichiara |
| `--da N --a M` | analizza solo le inquadrature da N a M |

## Com'è fatto il programma

| File | Cosa contiene |
|---|---|
| `mac/Murnau.swift`, `mac/compila.sh`, `mac/Info.plist` | l'app nativa per Mac, il suo costruttore e la sua scheda (tipi di file per *Apri con*) |
| `app.py` | il piccolo server interno che serve l'interfaccia (`interfaccia.html`); senza `--servizio` anche una finestra di ripiego con pywebview |
| `sessioni.py` | sessioni, materiali e cartelle di lavoro |
| `lavoro.py` | l'analisi lanciata dall'app: avanzamento, percentuale, tempo restante, esito |
| `murnau.py` | l'analisi vera e propria, divisa in fasi |
| `rilevatori.py` | OWLv2, volti e sguardo, Qwen3-VL e Gemma 4 tramite Ollama |
| `montaggio.py`, `aaf.py` | EDL, AAF, timecode, tagli e bande nere |
| `esporta.py` | marker per Avid e Resolve, foglio di calcolo, report |
| `archivi.py`, `progresso.py` | scrittura sicura dei file; righe `@@` dell'avanzamento |
| `banco.py` | banco di prova: le tue revisioni, le statistiche e la prova dei modelli |
| `coordinamento.py` | convivenza con Vertov e Ozu, analisi lanciate da Terminale |
| `aggiornamenti.py`, `pulizia.py` | aggiornamenti e Libera spazio |

I test automatici (non usano modelli né Ollama, durano un secondo):

```bash
~/.venvs/descrittore/bin/python -W ignore -m unittest discover -s tests
```

## Requisiti

Murnau usa lo stesso ambiente Python e lo stesso Ollama già installati sul Mac (`~/.venvs/descrittore`, modelli `qwen3-vl:30b-a3b-instruct` e `gemma4:26b`), ma è un'app a sé: ha dati, impostazioni e finestra propri, in `~/Library/Application Support/Murnau`.

## Limiti

- Un riflesso minuscolo o un boom che entra per pochi fotogrammi tra due campioni può sfuggire: la modalità accurata riduce il rischio.
- Un cavo della troupe nascosto tra gli oggetti di scena può sfuggire: Murnau cerca solo ciò che la troupe lascia, non le prese e i cavi d'arredo.
- Lo sguardo in macchina di un protagonista è difficile da riconoscere: Murnau lo propone solo entro 2°, come «da controllare», e può sfuggire.
- Uno sguardo in macchina voluto (una soggettiva, un a parte) viene segnalato come gli altri.
