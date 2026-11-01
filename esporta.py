"""Uscite di Murnau: foglio di calcolo, marker per Avid e Resolve, report HTML.

Le segnalazioni marcate come "falso allarme" nella revisione (revisione.json,
scritto dall'app) vengono escluse dai marker e dal report.
"""

import csv
import html
import json
import re
from pathlib import Path

import montaggio

# Colori dei marker di Media Composer per categoria.
COLORI_AVID = {"troupe": "red", "riflessi": "magenta", "sguardi": "yellow", "lavalier": "cyan",
               "prese_cavi": "blue"}
COLORI_RESOLVE = {"troupe": "ResolveColorRed", "riflessi": "ResolveColorPink", "sguardi": "ResolveColorYellow",
                  "lavalier": "ResolveColorCyan", "prese_cavi": "ResolveColorBlue"}


def leggi(out: Path) -> tuple:
    """(dati di segnalazioni.json, revisione {id: "confermato"|"falso", "note:"+id: testo})."""
    dati = json.loads((out / "segnalazioni.json").read_text(encoding="utf-8"))
    try:
        revisione = json.loads((out / "revisione.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        revisione = {}
    return dati, revisione


def valide(dati: dict, revisione: dict) -> list:
    return [s for s in dati["segnalazioni"] if revisione.get(s["id"]) != "falso"]


def _nota(s: dict, revisione: dict) -> str:
    nota = revisione.get("note:" + s["id"], "")
    testo = f'{s["titolo"]}: {s["descrizione"]}' + (f" — {nota}" if nota else "")
    return re.sub(r"\s+", " ", testo).strip()


def scrivi_csv(out: Path, dati: dict, revisione: dict) -> None:
    campi = ["numero", "tc", "tc_in", "tc_out", "categoria", "descrizione", "certezza", "stato", "nota",
             "clip", "scena", "fotogrammi_sospetti", "fotogrammi_totali", "immagine"]
    # Separatore ";" e BOM: si apre correttamente in Excel con impostazioni italiane.
    with (out / "segnalazioni.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campi, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for s in dati["segnalazioni"]:
            w.writerow({**s, "categoria": s["titolo"], "stato": revisione.get(s["id"], "da rivedere"),
                        "nota": revisione.get("note:" + s["id"], "")})


def scrivi_marker_avid(out: Path, dati: dict, revisione: dict) -> None:
    """Marker per Media Composer: finestra Markers > menu > Import Markers.

    Formato di testo di Avid separato da tabulazioni: utente, timecode, traccia,
    colore, commento, durata in fotogrammi.
    """
    righe = [f'Murnau\t{s["tc"]}\tV1\t{COLORI_AVID[s["categoria"]]}\t{_nota(s, revisione)}\t1'
             for s in valide(dati, revisione)]
    (out / "marker_avid.txt").write_text("\n".join(righe) + "\n", encoding="utf-8")


def scrivi_marker_resolve(out: Path, dati: dict, revisione: dict) -> None:
    """EDL di marker per DaVinci Resolve (Timeline > Importa > Marker della timeline da EDL)."""
    fps = dati["fps"]
    righe = ["TITLE: Murnau", "FCM: NON-DROP FRAME", ""]
    for n, s in enumerate(valide(dati, revisione), 1):
        tc = s["tc"]
        tc1 = montaggio.secondi_a_tc(montaggio.tc_a_secondi(tc, fps) + 1 / fps, fps)
        righe.append(f"{n:03d}  001      V     C        {tc} {tc1} {tc} {tc1}  ")
        righe.append(f' |C:{COLORI_RESOLVE[s["categoria"]]} |M:{_nota(s, revisione).replace("|", "/")} |D:1')
        righe.append("")
    (out / "marker_resolve.edl").write_text("\n".join(righe), encoding="utf-8")


def scrivi_report(out: Path, dati: dict, revisione: dict) -> None:
    """Report da aprire nel browser o da inviare (insieme alla cartella segnalazioni)."""
    e = html.escape
    elenco = valide(dati, revisione)
    categorie = {}
    for s in elenco:
        categorie.setdefault(s["categoria"], s["titolo"])
    filtri = "".join(f'<label><input type="checkbox" checked data-cat="{c}"> {e(t)} '
                     f'<span>{sum(1 for s in elenco if s["categoria"] == c)}</span></label>'
                     for c, t in categorie.items())
    schede = []
    for s in elenco:
        stato = revisione.get(s["id"])
        nota = revisione.get("note:" + s["id"], "")
        schede.append(f"""<article data-cat="{s['categoria']}" class="{s['categoria']}">
  <a href="{e(s['immagine'])}" target="_blank"><img src="{e(s['immagine'])}" loading="lazy" alt=""></a>
  <img class="dettaglio" src="{e(s['dettaglio'])}" loading="lazy" alt="">
  <div class="testo">
    <p class="tc">{e(s['tc'])} <span>inq. {s['numero']} · {e(s['tc_in'])}–{e(s['tc_out'])}</span></p>
    <p class="cat">{e(s['titolo'])}{' · <b>confermato</b>' if stato == 'confermato' else ''}</p>
    <p>{e(s['descrizione'])}</p>
    {f'<p class="nota">{e(nota)}</p>' if nota else ''}
    <p class="clip">{e(s['scena'] or s['clip'])} · certezza {e(s['certezza'])} · in {s['fotogrammi_sospetti']} fotogrammi su {s['fotogrammi_totali']}</p>
  </div>
</article>""")
    titolo = Path(dati["video"]).stem
    pagina = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Murnau · {e(titolo)}</title>
<style>
:root {{ --carta:#f2f0eb; --pannello:#fff; --inchiostro:#1d1d22; --tenue:#6b6b73; --linea:#dddad2; --accento:#c8372d; }}
@media (prefers-color-scheme: dark) {{ :root {{ --carta:#16161a; --pannello:#1f1f25; --inchiostro:#eceae4; --tenue:#a2a2aa; --linea:#34343c; --accento:#ff6b5e; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--carta); color:var(--inchiostro); font:15px/1.45 -apple-system,system-ui,sans-serif; }}
header {{ padding:20px 16px 12px; max-width:1200px; margin:auto; }}
h1 {{ margin:0 0 4px; font-size:24px; }} h1 b {{ color:var(--accento); }}
.info {{ color:var(--tenue); margin:0 0 12px; }}
.filtri {{ display:flex; flex-wrap:wrap; gap:8px 16px; }}
.filtri span {{ color:var(--tenue); }}
main {{ max-width:1200px; margin:auto; padding:0 16px 40px; display:grid; gap:14px; }}
article {{ display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,2fr); gap:12px; background:var(--pannello);
  border:1px solid var(--linea); border-left:5px solid var(--c); border-radius:10px; padding:10px; }}
article img {{ width:100%; border-radius:6px; display:block; }}
.dettaglio {{ object-fit:contain; max-height:260px; background:#000; }}
.testo p {{ margin:0 0 6px; }}
.tc {{ font:600 18px/1.2 ui-monospace,Menlo,monospace; }} .tc span {{ font:13px -apple-system,system-ui; color:var(--tenue); }}
.cat {{ font-weight:600; color:var(--c); }} .clip {{ color:var(--tenue); font-size:13px; }} .nota {{ font-style:italic; }}
.troupe {{ --c:#d23b30; }} .riflessi {{ --c:#b0389a; }} .sguardi {{ --c:#b8860b; }} .lavalier {{ --c:#178a9a; }} .prese_cavi {{ --c:#2f62c4; }}
@media (max-width:760px) {{ article {{ grid-template-columns:1fr; }} }}
</style></head><body>
<header><h1>Murnau · {e(titolo)}</h1>
<p class="info">{len(elenco)} segnalazioni su {dati['inquadrature']} inquadrature · analisi del {e(dati['creato'])}</p>
<div class="filtri">{filtri}</div></header>
<main>{''.join(schede) or '<p>Nessuna segnalazione.</p>'}</main>
<script>
document.querySelectorAll('.filtri input').forEach(c => c.addEventListener('change', () => {{
  const attive = [...document.querySelectorAll('.filtri input:checked')].map(x => x.dataset.cat);
  document.querySelectorAll('article').forEach(a => a.hidden = !attive.includes(a.dataset.cat));
}}));
</script></body></html>"""
    (out / "report_murnau.html").write_text(pagina, encoding="utf-8")


def tutto(out: Path) -> None:
    dati, revisione = leggi(out)
    scrivi_csv(out, dati, revisione)
    scrivi_marker_avid(out, dati, revisione)
    scrivi_marker_resolve(out, dati, revisione)
    scrivi_report(out, dati, revisione)
