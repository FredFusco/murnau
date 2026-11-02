"""I tre rilevatori di Murnau, tutti locali.

- Oggetti: OWLv2 (Google, licenza Apache 2.0) cerca microfoni, macchine da
  presa, stativi, prese e cavi in ogni fotogramma. È veloce ma impreciso:
  serve solo a trovare i sospetti.
- Volti: YuNet (OpenCV) trova i volti rivolti verso la macchina da presa e un
  modello di stima dello sguardo (L2CS addestrato su Gaze360, licenza MIT)
  misura di quanti gradi lo sguardo si discosta dall'obiettivo.
- Qwen3-VL tramite Ollama: esamina i fotogrammi con domande precise e poi
  verifica ogni sospetto su un ritaglio ingrandito. Solo ciò che supera la
  verifica diventa una segnalazione.
"""

import base64
import gc
import http.client
import json
import os
import time
import urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
OLLAMA_URL = OLLAMA + "/api/chat"
CONTESTO = 8192           # token di contesto chiesti a Ollama (deve essere lo stesso in ogni richiesta)
TENUTA = "5m"             # quanto Ollama tiene il modello in memoria dopo l'ultima richiesta di Murnau
MODELLO_PREDEFINITO = "qwen3-vl:30b-a3b-instruct"
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
    "prese_cavi": "Prese elettriche e cavi",
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
    ("prese_cavi", "presa elettrica", "an electrical wall outlet"),
    ("prese_cavi", "presa multipla", "a power strip"),
    ("prese_cavi", "cavo", "an electrical cable lying on the floor"),
    ("prese_cavi", "cavo", "a power cord"),
]
CATEGORIE_OGGETTI = {cat for cat, _, _ in RICERCHE}  # le categorie in cui OWLv2 cerca i sospetti
SOGLIA_OGGETTI = 0.19     # punteggio minimo OWLv2: basso apposta, la verifica scarta i falsi. A mezza
                          # precisione i punteggi oscillano di qualche centesimo: 0.19 trova tutto ciò che
                          # a precisione piena si trovava con 0.2
ALTEZZA_VOLTO = 0.07      # volti più piccoli (frazione dell'altezza) sono troppo lontani per lo sguardo
FRONTALITA = 0.36         # distanza tra gli occhi / larghezza del volto: sopra è frontale
SCARTO_NASO = 0.25        # spostamento del naso dal centro degli occhi, rispetto alla loro distanza
SOGLIA_SGUARDO = 8.0      # gradi tra lo sguardo e l'obiettivo sotto cui si segnala lo sguardo in macchina
SGUARDO_CERTO = 4.0       # sotto questa soglia la certezza è alta


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
            risultato.append({"riquadro": [round(max(0, x) / w, 4), round(max(0, y) / h, 4),
                                           round(min(w, x + bw) / w, 4), round(min(h, y + bh) / h, 4)],
                              "frontalita": round(frontalita - scarto, 3),
                              "angolo": round(float(np.hypot(orizzontale, verticale)), 1),
                              "orizzontale": round(orizzontale, 1), "verticale": round(verticale, 1)})
        return risultato


# --------------------------------------------------------------------------
# Qwen3-VL
# --------------------------------------------------------------------------

def chiedi(modello: str, prompt: str, immagini: list, schema: dict, tentativi: int = 3,
           attesa_ollama: float = 120) -> dict:
    """La risposta di Qwen3-VL, già letta secondo lo schema JSON. RuntimeError se non arriva.

    Se Ollama si chiude a metà (per esempio va in crash), lo si riavvia e lo si aspetta fino a
    `attesa_ollama` secondi prima di riprovare: un'analisi di ore non si perde per un singhiozzo.
    """
    corpo = json.dumps({
        "model": modello,
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(i if isinstance(i, bytes) else Path(i).read_bytes()).decode()
                                 for i in immagini]}],
        "format": schema, "stream": False, "think": False, "keep_alive": TENUTA,
        "options": {"temperature": 0, "num_ctx": CONTESTO, "num_predict": 900},
    }).encode()
    for tentativo in range(1, tentativi + 1):
        try:
            req = urllib.request.Request(OLLAMA_URL, data=corpo, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                risposta = json.load(r)
            if risposta.get("done_reason") == "length":
                # Con temperatura 0 riprovare darebbe la stessa risposta troncata: inutile.
                raise RuntimeError("Il modello ha dato una risposta troppo lunga, rimasta a metà.")
            return json.loads(risposta["message"]["content"])
        # OSError comprende la connessione rifiutata o chiusa e il tempo scaduto; ValueError il JSON
        # non valido; HTTPException le risposte interrotte a metà.
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as e:
            if tentativo == tentativi:
                raise RuntimeError(f"Il modello non ha risposto correttamente: {e}") from e
            if attesa_ollama > 0 and not ollama_acceso():
                avvia_ollama(attesa_ollama)
            time.sleep(3)


# Cosa cercare nell'esame di ogni fotogramma, per categoria.
VOCI_ESAME = {
    "troupe": [
        ("boom", "un microfono boom: un'asta lunga e sottile con un microfono cilindrico o con una cuffia "
                 "antivento pelosa, di solito entra dal bordo alto dell'inquadratura"),
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
    # Gli sguardi in macchina non si chiedono a Qwen3-VL (giudica male la direzione degli occhi):
    # li misura il modello dello sguardo.
    "sguardi": [],
    "lavalier": [
        ("lavalier", "un radiomicrofono lavalier: piccola capsula, di solito nera, fissata al colletto o alla "
                     "camicia di un attore, o il suo filo visibile sul corpo"),
    ],
    "prese_cavi": [
        ("presa", "una presa elettrica, una presa multipla o una scatola elettrica su una parete o a terra"),
        ("cavo", "un cavo elettrico che corre a terra o lungo una parete"),
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
                "lampada, lampadario o luce d'arredo", "lampione o luce stradale", "luci di scena o di un locale",
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
                    "interruttore della luce", "presa elettrica a muro", "presa multipla", "scatola o quadro elettrico",
                    "cavo elettrico"],
                   {"presa elettrica a muro", "presa multipla", "scatola o quadro elettrico", "cavo elettrico"}),
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
