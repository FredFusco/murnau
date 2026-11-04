"""I tre rilevatori di Murnau, tutti locali.

- Oggetti: OWLv2 (Google, licenza Apache 2.0) cerca microfoni, macchine da
  presa, stativi, prese e cavi in ogni fotogramma. È veloce ma impreciso:
  serve solo a trovare i sospetti.
- Volti: YuNet (OpenCV) trova i volti rivolti verso la macchina da presa e un
  modello di stima dello sguardo (L2CS addestrato su Gaze360, licenza MIT)
  misura di quanti gradi lo sguardo si discosta dall'obiettivo.
- Due modelli con visione tramite Ollama: Qwen3-VL esamina i fotogrammi con
  domande precise (è il più attento ai dettagli piccoli); Gemma 4 verifica ogni
  sospetto su un ritaglio ingrandito (veloce, ma generoso) e Qwen3-VL dà la
  seconda opinione su ciò che Gemma accetta (prudente): se sono d'accordo è una
  segnalazione, se no è «da controllare». La scelta viene da un confronto di 12
  modelli gratuiti sulle segnalazioni rivedute e da una prova su un rullo (2026-11).
"""

import base64
import difflib
import gc
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
OLLAMA_URL = OLLAMA + "/api/chat"
CONTESTO = 8192           # token di contesto chiesti a Ollama (deve essere lo stesso in ogni richiesta)
TENUTA = "5m"             # quanto Ollama tiene il modello in memoria dopo l'ultima richiesta di Murnau
MODELLO_ESAME = "qwen3-vl:30b-a3b-instruct"   # solo di Murnau (Vertov dal 2026-11-04 usa Gemma 4)
MODELLO_VERIFICA = "gemma4:26b"                 # lo usano anche Ozu e Vertov
MODELLI = {MODELLO_ESAME: "Qwen3-VL, esamina i fotogrammi",
           MODELLO_VERIFICA: "Gemma 4, verifica i sospetti (lo usano anche Ozu e Vertov)"}
MODELLO_PREDEFINITO = MODELLO_ESAME
# Cambia quando cambiano le domande fatte ai modelli o le ricerche di OWLv2: i risultati archiviati con
# domande diverse non si riusano (vedi murnau.Analisi.archivi).
VERSIONE_DOMANDE = 2
VERSIONE_SGUARDI = 2   # cambia quando cambia la misura dello sguardo
MODELLO_OGGETTI = "google/owlv2-base-patch16-ensemble"
CARTELLA_MODELLI = Path.home() / "Library" / "Application Support" / "Murnau" / "modelli"
# I modelli di Hugging Face di Murnau stanno qui e non nella cache comune (~/.cache/huggingface):
# così la pulizia di altre app non li scambia per modelli inutilizzati.
CACHE_HF = CARTELLA_MODELLI / "huggingface"
URL_YUNET = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
URL_SGUARDO = "https://github.com/yakhyo/gaze-estimation/releases/download/weights/resnet34_gaze.onnx"

CATEGORIE = {
    "troupe": "Troupe o attrezzatura in campo",
    "riflessi": "Troupe nei riflessi",
    "sguardi": "Sguardo in macchina",
    "lavalier": "Microfono lavalier visibile",
    "prese_cavi": "Cavi e prolunghe della troupe",
}

# Domande per OWLv2: (categoria, nome italiano, testo cercato).
RICERCHE = [
    ("troupe", "microfono boom", "a boom microphone on a pole"),
    ("troupe", "microfono boom", "a furry microphone windscreen"),
    ("troupe", "macchina da presa", "a professional film camera"),
    ("troupe", "cavalletto", "a camera tripod"),
    ("troupe", "stativo per luci", "a lighting stand"),
    ("troupe", "proiettore da set", "a film studio light"),
    ("lavalier", "microfono lavalier", "a lavalier microphone clipped to clothing"),
    ("prese_cavi", "cavo della troupe", "a thick black cable lying on the floor"),
    ("prese_cavi", "prolunga", "an extension cord reel"),
    ("prese_cavi", "presa multipla", "a power strip on the floor"),
    ("prese_cavi", "nastro adesivo", "gaffer tape on the floor"),
]
CATEGORIE_OGGETTI = {cat for cat, _, _ in RICERCHE}  # le categorie in cui OWLv2 cerca i sospetti
SOGLIA_OGGETTI = 0.19     # punteggio minimo OWLv2: basso apposta, la verifica scarta i falsi. A mezza
                          # precisione i punteggi oscillano di qualche centesimo: 0.19 trova tutto ciò che
                          # a precisione piena si trovava con 0.2
ALTEZZA_VOLTO = 0.07      # volti più piccoli (frazione dell'altezza) sono troppo lontani per lo sguardo
FRONTALITA = 0.36         # distanza tra gli occhi / larghezza del volto: sopra è frontale
SCARTO_NASO = 0.25        # spostamento del naso dal centro degli occhi, rispetto alla loro distanza
# Gradi tra lo sguardo e l'obiettivo sotto cui si segnala lo sguardo in macchina. La misura oscilla di
# qualche grado anche per pochi pixel di differenza: per questo si prende la mediana su ritagli appena
# diversi (MOSSE_SGUARDO). Anche così, sui volti grandi non distingue il protagonista che guarda il partner
# appena accanto alla macchina (18 falsi allarmi su 20 nel rullo di prova) da chi guarda nell'obiettivo:
# quelli si segnalano solo entro 2° e sempre «da controllare». Sulle comparse, i volti piccoli sullo
# sfondo, uno sguardo verso l'obiettivo è quasi sempre un errore: lì la soglia è larga.
SOGLIA_SGUARDO = 2.0
SOGLIA_SGUARDO_COMPARSE = 10.0
VOLTO_PICCOLO = 0.10      # altezza del volto (frazione del fotogramma) sotto cui è di una comparsa
SGUARDO_CERTO = 4.0       # comparse sotto questa soglia: certezza alta
# (spostamento orizzontale, verticale, scala) dei ritagli del volto su cui si misura lo sguardo.
MOSSE_SGUARDO = [(0, 0, 1), (.04, 0, 1), (-.04, 0, 1), (0, .04, 1), (0, -.04, 1), (0, 0, 1.08), (0, 0, .92)]


def comparsa(riquadro: list) -> bool:
    return riquadro[3] - riquadro[1] < VOLTO_PICCOLO


def soglia_sguardo(riquadro: list) -> float:
    """Gradi massimi per segnalare lo sguardo di quel volto: larga per le comparse, severa per gli altri."""
    return SOGLIA_SGUARDO_COMPARSE if comparsa(riquadro) else SOGLIA_SGUARDO


# --------------------------------------------------------------------------
# OWLv2
# --------------------------------------------------------------------------

class Oggetti:
    def __init__(self, categorie: set):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
        self.torch = torch
        self.ricerche = [r for r in RICERCHE if r[0] in categorie]
        self.dispositivo = "mps" if torch.backends.mps.is_available() else "cpu"
        # Sulla GPU del Mac a mezza precisione: quasi tre volte più veloce, stessi oggetti trovati.
        self.tipo = torch.float16 if self.dispositivo == "mps" else torch.float32
        try:
            # Dopo il primo scaricamento il modello si legge solo dal disco, senza collegarsi a internet.
            self.proc = Owlv2Processor.from_pretrained(MODELLO_OGGETTI, cache_dir=CACHE_HF, local_files_only=True)
            modello = Owlv2ForObjectDetection.from_pretrained(MODELLO_OGGETTI, cache_dir=CACHE_HF, local_files_only=True)
        except OSError:
            print("Scarico il rilevatore di oggetti OWLv2 (una volta sola, circa 600 MB)...")
            self.proc = Owlv2Processor.from_pretrained(MODELLO_OGGETTI, cache_dir=CACHE_HF)
            modello = Owlv2ForObjectDetection.from_pretrained(MODELLO_OGGETTI, cache_dir=CACHE_HF)
        self.modello = modello.to(self.dispositivo, dtype=self.tipo).eval()

    def libera(self) -> None:
        """Restituisce la memoria della GPU (circa 1 GB): serve a Qwen3-VL, che viene dopo."""
        del self.modello
        gc.collect()
        if self.dispositivo == "mps":
            self.torch.mps.empty_cache()

    def cerca(self, percorso: Path) -> list:
        """[{categoria, oggetto, punteggio, riquadro (0-1)}] nel fotogramma (OSError se non si legge)."""
        if not self.ricerche:
            return []
        from PIL import Image
        with Image.open(percorso) as aperta:
            img = aperta.convert("RGB")
        w, h = img.size
        testi = [[r[2] for r in self.ricerche]]
        ingresso = self.proc(text=testi, images=img, return_tensors="pt").to(self.dispositivo)
        ingresso["pixel_values"] = ingresso["pixel_values"].to(self.tipo)
        with self.torch.no_grad():
            uscita = self.modello(**ingresso)
        uscita.logits, uscita.pred_boxes = uscita.logits.float(), uscita.pred_boxes.float()
        # OWLv2 lavora su un quadrato: le coordinate sono rispetto al lato maggiore.
        lato = max(w, h)
        res = self.proc.post_process_grounded_object_detection(
            uscita, threshold=SOGLIA_OGGETTI, target_sizes=self.torch.tensor([[lato, lato]]))[0]
        trovati = []
        for punteggio, etichetta, riquadro in zip(res["scores"], res["labels"], res["boxes"]):
            x0, y0, x1, y1 = (float(v) for v in riquadro)
            x0, x1 = max(0, x0) / w, min(w, x1) / w
            y0, y1 = max(0, y0) / h, min(h, y1) / h
            if x1 - x0 <= 0 or y1 - y0 <= 0 or (x1 - x0) * (y1 - y0) > 0.6:
                continue  # i riquadri che coprono quasi tutto il fotogramma non indicano un oggetto
            cat, nome, _ = self.ricerche[int(etichetta)]
            trovati.append({"categoria": cat, "oggetto": nome, "punteggio": round(float(punteggio), 3),
                            "riquadro": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]})
        return trovati


# --------------------------------------------------------------------------
# Volti
# --------------------------------------------------------------------------

def _modello(nome: str, url: str, descrizione: str) -> Path:
    CARTELLA_MODELLI.mkdir(parents=True, exist_ok=True)
    percorso = CARTELLA_MODELLI / nome
    if not percorso.exists():
        print(f"Scarico {descrizione} (una volta sola)...")
        urllib.request.urlretrieve(url, percorso.with_suffix(".parziale"))
        percorso.with_suffix(".parziale").rename(percorso)
    return percorso


class Volti:
    def __init__(self):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        rilevatore = _modello("yunet.onnx", URL_YUNET, "il modello dei volti (YuNet)")
        self.rilevatore = cv2.FaceDetectorYN.create(str(rilevatore), "", (320, 320), 0.75)
        self.sguardo = cv2.dnn.readNetFromONNX(str(_modello("sguardo_resnet34.onnx", URL_SGUARDO,
                                                             "il modello dello sguardo (L2CS, 85 MB)")))

    def _angoli(self, volto) -> tuple:
        """(orizzontale, verticale) dello sguardo in gradi; (0, 0) = dritto nell'obiettivo."""
        np = self.np
        blob = self.cv2.dnn.blobFromImage(volto, 1 / 255, (448, 448), swapRB=True)
        blob = (blob - np.array([0.485, 0.456, 0.406]).reshape(1, 3, 1, 1)) / np.array([0.229, 0.224, 0.225]).reshape(1, 3, 1, 1)
        self.sguardo.setInput(blob.astype(np.float32))
        uscite = self.sguardo.forward(["yaw", "pitch"])
        # 90 classi da 4 gradi tra -180 e +180: si prende il valore atteso.
        angoli = []
        for logit in uscite:
            e = np.exp(logit[0] - logit[0].max())
            angoli.append(float((e / e.sum() * np.arange(90)).sum() * 4 - 180))
        return tuple(angoli)

    def _ritagli(self, img, x, y, bw, bh) -> list:
        h, w = img.shape[:2]
        ritagli = []
        for dx, dy, scala in MOSSE_SGUARDO:
            cx, cy = x + bw / 2 + dx * bw, y + bh / 2 + dy * bh
            a0, b0 = int(max(0, cx - bw * scala / 2)), int(max(0, cy - bh * scala / 2))
            a1, b1 = int(min(w, cx + bw * scala / 2)), int(min(h, cy + bh * scala / 2))
            if a1 > a0 and b1 > b0:
                ritagli.append(img[b0:b1, a0:a1])
        return ritagli

    def frontali(self, percorso: Path) -> list:
        """Volti rivolti verso la macchina da presa: [{riquadro (0-1), frontalita, angolo, orizzontale, verticale}].

        OSError se il fotogramma non si legge: non è un fotogramma senza volti."""
        np = self.np
        img = self.cv2.imread(str(percorso))
        if img is None:
            raise OSError(f"fotogramma illeggibile: {Path(percorso).name}")
        h, w = img.shape[:2]
        self.rilevatore.setInputSize((w, h))
        _, trovati = self.rilevatore.detect(img)
        risultato = []
        for f in (trovati if trovati is not None else []):
            x, y, bw, bh = (float(v) for v in f[:4])
            if bh / h < ALTEZZA_VOLTO:
                continue
            occhi = float(np.hypot(f[4] - f[6], f[5] - f[7]))
            if occhi < 1:
                continue
            scarto = float(abs(f[8] - (f[4] + f[6]) / 2)) / occhi
            frontalita = occhi / bw
            if frontalita < FRONTALITA or scarto > SCARTO_NASO:
                continue
            volto = img[int(max(0, y)):int(min(h, y + bh)), int(max(0, x)):int(min(w, x + bw))]
            if volto.size == 0:
                continue
            orizzontale, verticale = self._angoli(volto)
            if np.hypot(orizzontale, verticale) <= SOGLIA_SGUARDO_COMPARSE + 4:
                # Vicino alla soglia: mediana su ritagli appena spostati, la misura singola oscilla troppo.
                misure = [self._angoli(r) for r in self._ritagli(img, x, y, bw, bh)]
                orizzontale = float(np.median([m[0] for m in misure]))
                verticale = float(np.median([m[1] for m in misure]))
            risultato.append({"riquadro": [round(max(0, x) / w, 4), round(max(0, y) / h, 4),
                                           round(min(w, x + bw) / w, 4), round(min(h, y + bh) / h, 4)],
                              "frontalita": round(frontalita - scarto, 3),
                              "angolo": round(float(np.hypot(orizzontale, verticale)), 1),
                              "orizzontale": round(orizzontale, 1), "verticale": round(verticale, 1)})
        return risultato


# --------------------------------------------------------------------------
# Modelli con visione (Ollama)
# --------------------------------------------------------------------------

# Modelli che Ollama fa girare senza "risposta a schema" (per esempio quelli col motore MLX, come
# Gemma 4): a loro lo schema si descrive nel testo e la risposta si controlla dopo.
SENZA_SCHEMA = set()


def _schema_a_parole(schema: dict, rientro: str = "") -> list:
    righe = []
    for chiave, v in schema.get("properties", {}).items():
        if v.get("type") == "object":
            righe += [f'{rientro}"{chiave}": {{', *_schema_a_parole(v, rientro + "  "), f"{rientro}}}"]
        elif "enum" in v:
            righe.append(f'{rientro}"{chiave}": una di queste stringhe, copiata esattamente: '
                         + " | ".join(json.dumps(e, ensure_ascii=False) for e in v["enum"]))
        elif v.get("type") == "array":
            righe.append(f'{rientro}"{chiave}": lista di numeri interi')
        else:
            righe.append(f'{rientro}"{chiave}": {v.get("type", "string")}')
    return righe


def _secondo_schema(valore, schema: dict):
    """La risposta in testo resa conforme allo schema: un'opzione scritta un po' diversa diventa la più simile."""
    if schema.get("type") == "object":
        valore = valore if isinstance(valore, dict) else {}
        return {k: _secondo_schema(valore.get(k), sotto) for k, sotto in schema.get("properties", {}).items()}
    if "enum" in schema and valore not in schema["enum"]:
        return _opzione_piu_vicina(str(valore), schema["enum"])
    if schema.get("type") == "boolean":
        return valore is True or str(valore).lower() in ("true", "sì", "si")
    if schema.get("type") == "array":
        return valore if isinstance(valore, list) else []
    return valore if valore is not None else ""


def _opzione_piu_vicina(risposta: str, opzioni: list) -> str:
    """L'opzione che contiene più parole della risposta; nel dubbio la più prudente.

    La più prudente è la prima delle classi (sempre «non riconoscibile» o simile) o, per la certezza,
    «bassa»: una risposta mal scritta non deve mai diventare un errore segnalato."""
    parole = lambda t: set(re.findall(r"\w+", t.lower()))
    cercate = parole(risposta)
    prudente = "bassa" if "bassa" in opzioni else opzioni[0]
    if not cercate:
        return prudente
    punteggi = [(len(cercate & parole(o)) / len(cercate), difflib.SequenceMatcher(None, risposta.lower(), o.lower()).ratio(), o)
                for o in opzioni]
    copertura, _, migliore = max(punteggi)
    pari = [o for c, _, o in punteggi if c == copertura]
    return migliore if copertura >= 0.6 and len(pari) == 1 else prudente


def _corpo(modello: str, prompt: str, immagini: list, schema: dict) -> bytes:
    con_schema = modello not in SENZA_SCHEMA
    if not con_schema:
        prompt += ("\n\nRispondi SOLO con un oggetto JSON, senza altro testo, con queste chiavi:\n"
                   + "\n".join(_schema_a_parole(schema)))
    return json.dumps({
        "model": modello,
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(i if isinstance(i, bytes) else Path(i).read_bytes()).decode()
                                 for i in immagini]}],
        **({"format": schema} if con_schema else {}), "stream": False, "think": False, "keep_alive": TENUTA,
        "options": {"temperature": 0, "num_ctx": CONTESTO, "num_predict": 900},
    }).encode()


def chiedi(modello: str, prompt: str, immagini: list, schema: dict, tentativi: int = 3,
           attesa_ollama: float = 120) -> dict:
    """La risposta del modello, già letta secondo lo schema JSON. RuntimeError se non arriva.

    Se Ollama si chiude a metà (per esempio va in crash), lo si riavvia e lo si aspetta fino a
    `attesa_ollama` secondi prima di riprovare: un'analisi di ore non si perde per un singhiozzo.
    """
    tentativo = 0
    while tentativo < tentativi:
        tentativo += 1
        try:
            req = urllib.request.Request(OLLAMA_URL, data=_corpo(modello, prompt, immagini, schema),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                risposta = json.load(r)
            if risposta.get("done_reason") == "length":
                # Con temperatura 0 riprovare darebbe la stessa risposta troncata: inutile.
                raise RuntimeError("Il modello ha dato una risposta troppo lunga, rimasta a metà.")
            testo = risposta["message"]["content"]
            if modello in SENZA_SCHEMA:
                trovato = re.search(r"\{.*\}", testo, re.S)
                if not trovato:
                    raise ValueError(f"nessun JSON nella risposta: {testo[:80]!r}")
                return _secondo_schema(json.loads(trovato.group(0)), schema)
            return json.loads(testo)
        except urllib.error.HTTPError as e:
            if e.code == 501 and modello not in SENZA_SCHEMA:
                SENZA_SCHEMA.add(modello)  # «structured output is unavailable»: si riprova subito a parole
                tentativo -= 1
                continue
            if tentativo == tentativi:
                raise RuntimeError(f"Il modello non ha risposto correttamente: {e}") from e
            time.sleep(3)
        # OSError comprende la connessione rifiutata o chiusa e il tempo scaduto; ValueError il JSON
        # non valido; HTTPException le risposte interrotte a metà.
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as e:
            if tentativo == tentativi:
                raise RuntimeError(f"Il modello non ha risposto correttamente: {e}") from e
            if attesa_ollama > 0 and not ollama_acceso():
                avvia_ollama(attesa_ollama)
            time.sleep(3)


_YUNET = []  # il rilevatore dei volti per lo zoom, creato la prima volta che serve


def petti(img) -> list:
    """(x0, y0, x1, y1) in pixel del petto sotto ogni volto abbastanza grande: dove si attacca il lavalier."""
    import cv2
    if not _YUNET:
        _YUNET.append(cv2.FaceDetectorYN.create(str(_modello("yunet.onnx", URL_YUNET, "il modello dei volti (YuNet)")),
                                                "", (320, 320), 0.7))
    h, w = img.shape[:2]
    _YUNET[0].setInputSize((w, h))
    _, volti = _YUNET[0].detect(img)
    riquadri = []
    for f in (volti if volti is not None else []):
        x, y, bw, bh = (float(v) for v in f[:4])
        if bh / h < ALTEZZA_VOLTO:
            continue
        # Sotto il mento, largo il doppio del volto: colletto, bavero, scollo e camicia.
        x0, x1 = int(max(0, x - .6 * bw)), int(min(w, x + 1.6 * bw))
        y0, y1 = int(min(h, y + .9 * bh)), int(min(h, y + 2.6 * bh))
        if x1 - x0 > 20 and y1 - y0 > 20:
            riquadri.append((x0, y0, x1, y1))
    return riquadri


def esamina_zoom(modello: str, fotogramma: Path) -> list:
    """Esame dei lavalier sul petto ingrandito di ogni personaggio, con i riquadri riportati al fotogramma.

    Nel fotogramma intero un lavalier è di pochi pixel: sul banco di prova l'esame normale ne vedeva 1 su
    8, con lo zoom 4 su 8. Costa una chiamata al modello per personaggio: si fa solo in modalità accurata.
    OSError se il fotogramma non si legge."""
    import cv2
    img = cv2.imread(str(fotogramma))
    if img is None:
        raise OSError(f"fotogramma illeggibile: {Path(fotogramma).name}")
    h, w = img.shape[:2]
    trovati = []
    for x0, y0, x1, y1 in petti(img):
        ritaglio = cv2.imencode(".jpg", img[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()
        cw, ch = x1 - x0, y1 - y0
        for r in esamina(modello, ritaglio, {"lavalier"}):
            a0, b0, a1, b1 = r["riquadro"] or (0, 0, 1, 1)
            r["riquadro"] = [round((x0 + a0 * cw) / w, 4), round((y0 + b0 * ch) / h, 4),
                             round((x0 + a1 * cw) / w, 4), round((y0 + b1 * ch) / h, 4)]
            trovati.append(r)
    return trovati


# Cosa cercare nell'esame di ogni fotogramma, per categoria.
VOCI_ESAME = {
    "troupe": [
        ("boom", "un microfono boom della troupe: un'asta lunga e sottile con un microfono cilindrico o con una "
                 "cuffia antivento pelosa, che di solito entra dal bordo alto dell'inquadratura (NON un microfono "
                 "tenuto in mano da un personaggio o su un'asta da palco: quelli fanno parte della scena)"),
        ("attrezzatura", "una macchina da presa professionale, un cavalletto, uno stativo per luci, un pannello "
                         "luminoso o un riflettore da set (NON lampade d'arredo, luci di scena di una discoteca "
                         "o tralicci di un palco che fanno parte dell'ambiente)"),
        ("persona_troupe", "un membro della troupe al lavoro: operatore con la macchina da presa, fonico con "
                           "cuffie o asta, persona col ciak (NON comparse, passanti o personaggi)"),
    ],
    "riflessi": [
        ("riflesso", "un riflesso (in specchi, vetri, finestre, occhiali, carrozzerie, superfici lucide) in cui "
                     "si vede chiaramente una macchina da presa, un operatore, un boom o un riflettore da set"),
    ],
    # Gli sguardi in macchina non si chiedono ai modelli con visione: nessuno dei 12 provati distingue chi
    # guarda nell'obiettivo da chi guarda un personaggio accanto alla macchina. Li misura il modello dello sguardo.
    "sguardi": [],
    "lavalier": [
        ("lavalier", "un radiomicrofono lavalier: piccola capsula, di solito nera, fissata al colletto o alla "
                     "camicia di un attore, o il suo filo visibile sul corpo"),
    ],
    # Solo ciò che lascia la troupe: prese a muro e cavi degli oggetti di scena sono arredo (scelta
    # dell'utente dopo la revisione: 4 segnalazioni di prese a muro su 4 erano falsi allarmi).
    "prese_cavi": [
        ("cavo_troupe", "un cavo della troupe: cavi spessi di luci, macchina da presa o audio che corrono a terra, "
                        "prolunghe, multiprese o nastro adesivo da set (NON prese a muro, interruttori o il cavo di "
                        "una lampada, di un elettrodomestico o di un computer che fanno parte della scena)"),
    ],
}
VOCE_CATEGORIA = {voce: cat for cat, voci in VOCI_ESAME.items() for voce, _ in voci}


def esamina(modello: str, fotogramma: Path, categorie: set) -> list:
    """Esame scettico di un fotogramma: [{categoria, voce, descrizione, riquadro (0-1)}]."""
    voci = [(v, d) for c in CATEGORIE if c in categorie for v, d in VOCI_ESAME[c]]
    if not voci:
        return []
    voce_schema = {"type": "object", "properties": {
        "cosa_vedo": {"type": "string"}, "presente": {"type": "boolean"},
        "riquadro": {"type": "array", "items": {"type": "integer"}}},
        "required": ["cosa_vedo", "presente", "riquadro"]}
    schema = {"type": "object", "properties": {v: voce_schema for v, _ in voci}, "required": [v for v, _ in voci]}
    prompt = ("Fotogramma di un film già montato. In quasi tutti i fotogrammi NON ci sono errori: rispondi "
              "presente=true solo se l'elemento è chiaramente e inequivocabilmente visibile, e in cosa_vedo "
              "descrivi dove si trova e com'è fatto (forma, colore, posizione). Se non sai descrivere un oggetto "
              "concreto, presente=false. Ombre, sagome sfocate, persone sullo sfondo e oggetti di arredo non "
              "bastano. Per ogni elemento presente, riquadro = [x1, y1, x2, y2] in scala 0-1000. Per ogni elemento "
              "assente scrivi cosa_vedo = \"\" e riquadro = [], senza spiegazioni.\n"
              "Per ogni voce:\n" + "\n".join(f"- {v}: c'è {d}?" for v, d in voci))
    risposta = chiedi(modello, prompt, [fotogramma], schema)
    trovati = []
    for v, _ in voci:
        r = risposta.get(v) or {}
        if not r.get("presente"):
            continue
        b = r.get("riquadro") or []
        riquadro = None
        if len(b) == 4 and b[2] > b[0] and b[3] > b[1]:
            riquadro = [round(min(max(x, 0), 1000) / 1000, 4) for x in b]
        trovati.append({"categoria": VOCE_CATEGORIA[v], "voce": v,
                        "descrizione": r.get("cosa_vedo", ""), "riquadro": riquadro})
    return trovati


# Per ogni categoria: la domanda e le risposte possibili. Il modello sceglie che cosa vede tra
# opzioni precise; è il programma a decidere se è un errore (un sì/no diretto è poco affidabile).
VERIFICHE = {
    "troupe": ("Che cosa c'è nel riquadro rosso (ingrandito nella seconda immagine)?",
               ["forma sfocata non riconoscibile", "oggetto di scena o d'arredo", "persona che fa parte della scena",
                "microfono tenuto in mano da un personaggio (di scena)",
                "microfono su asta da palco o da tavolo (di scena)", "lampada, lampadario o luce d'arredo", "lampione o luce stradale", "luci di scena o di un locale",
                "telecamera di sorveglianza, sensore o impianto fisso", "traliccio, colonna o elemento architettonico",
                "microfono boom o asta microfonica", "macchina da presa professionale", "cavalletto o stativo",
                "proiettore o pannello luminoso da set", "operatore o membro della troupe al lavoro"],
               {"microfono boom o asta microfonica", "macchina da presa professionale", "cavalletto o stativo",
                "proiettore o pannello luminoso da set", "operatore o membro della troupe al lavoro"}),
    "riflessi": ("Che cosa si vede riflesso nella superficie dentro il riquadro rosso (ingrandito nella seconda "
                 "immagine)?",
                 ["riflesso non riconoscibile", "nessun riflesso", "solo l'ambiente o i personaggi",
                  "macchina da presa o operatore riflessi", "microfono boom riflesso", "luci da set riflesse"],
                 {"macchina da presa o operatore riflessi", "microfono boom riflesso", "luci da set riflesse"}),
    "lavalier": ("Che cosa c'è nel riquadro rosso (ingrandito nella seconda immagine)?",
                 ["oggetto non riconoscibile", "gioiello o collana", "bottone o cucitura", "parte del vestito",
                  "ombra o piega", "radiomicrofono lavalier (piccola capsula con filo)", "filo di un microfono sul corpo"],
                 {"radiomicrofono lavalier (piccola capsula con filo)", "filo di un microfono sul corpo"}),
    "prese_cavi": ("Che cosa c'è nel riquadro rosso (ingrandito nella seconda immagine)?",
                   ["oggetto non riconoscibile", "altro oggetto", "macchia, foro o ombra sulla parete",
                    "presa elettrica a muro o interruttore",
                    "cavo di un oggetto di scena (lampada, elettrodomestico, computer)",
                    "cavo della troupe a terra (luci, macchina da presa, audio)",
                    "prolunga o multipresa della troupe", "nastro adesivo da set a terra"],
                   {"cavo della troupe a terra (luci, macchina da presa, audio)", "prolunga o multipresa della troupe",
                    "nastro adesivo da set a terra"}),
}


def verifica(modello: str, categoria: str, immagini: list) -> dict:
    """Seconda opinione su un sospetto: {cosa_vedo, classe, errore, certezza}.

    `immagini` sono JPEG in memoria: il fotogramma con il riquadro rosso e il ritaglio
    ingrandito (per gli sguardi, solo il volto).
    """
    domanda, opzioni, errori = VERIFICHE[categoria]
    schema = {"type": "object", "properties": {
        "cosa_vedo": {"type": "string"}, "classe": {"type": "string", "enum": opzioni},
        "certezza": {"type": "string", "enum": ["alta", "media", "bassa"]}},
        "required": ["cosa_vedo", "classe", "certezza"]}
    prompt = ("Sei il controllo qualità di un film montato. " + domanda + " Descrivi prima in cosa_vedo, in "
              "italiano e in una o due frasi concrete, ciò che vedi esattamente; poi in classe scegli l'opzione "
              "che corrisponde alla tua descrizione. Se non sei sicuro, scegli l'opzione più prudente "
              "(non riconoscibile, sfocato, d'arredo).")
    r = chiedi(modello, prompt, immagini, schema)
    r["errore"] = r.get("classe") in errori and r.get("certezza") != "bassa"
    if r["errore"]:
        # Controllo di coerenza: la scelta deve corrispondere a ciò che il modello stesso ha descritto
        # (a volte descrive un lampione e poi sceglie "operatore"). Domanda solo testuale, rapida.
        controllo = chiedi(modello, "Ecco la descrizione di un dettaglio di un fotogramma:\n«" + r.get("cosa_vedo", "")
                           + "»\nScegli l'opzione che corrisponde a questa descrizione. Se la descrizione è "
                           "incerta o dice che non si riconosce, scegli l'opzione di oggetto non riconoscibile.",
                           [], {"type": "object", "properties": {"classe": {"type": "string", "enum": opzioni}},
                                "required": ["classe"]})
        r["classe_controllo"] = controllo.get("classe")
        r["errore"] = r["classe_controllo"] in errori
    return r


def modello_caricato(modello: str) -> bool:
    """True se Ollama ha già il modello in memoria con il contesto di Murnau (nessuna attesa).

    Se un'altra app (Vertov, Ozu) l'ha caricato con un contesto diverso, Ollama lo ricarica
    alla prima richiesta di Murnau: conta come non caricato.
    """
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=3) as r:
            return any(m["name"] == modello and m.get("context_length", CONTESTO) == CONTESTO
                       for m in json.load(r).get("models", []))
    except Exception:
        return False


def carica_modello(modello: str) -> None:
    """Porta il modello in memoria con il contesto che usa Murnau, senza fargli generare nulla."""
    corpo = json.dumps({"model": modello, "keep_alive": TENUTA, "options": {"num_ctx": CONTESTO}}).encode()
    try:
        req = urllib.request.Request(OLLAMA + "/api/generate", data=corpo,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=600).read()
    except Exception as e:
        print(f"Caricamento del modello non riuscito ({e}): ci riprovo con il primo esame.")


def _modelli_installati():
    """I nomi dei modelli installati in Ollama, o None se Ollama è spento."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


def ollama_acceso() -> bool:
    return _modelli_installati() is not None


def modello_installato(modello: str) -> bool:
    return any(nome.startswith(modello) for nome in _modelli_installati() or [])


def avvia_ollama(attesa: float = 30) -> None:
    """Avvia Ollama in locale se non è già acceso e aspetta fino a `attesa` secondi che risponda."""
    import shutil
    import subprocess
    if ollama_acceso():
        return
    if os.path.exists("/Applications/Ollama.app"):
        subprocess.Popen(["open", "-g", "-a", "Ollama"])
    elif shutil.which("ollama"):
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    limite = time.monotonic() + attesa
    while time.monotonic() < limite:
        time.sleep(1)
        if ollama_acceso():
            return
