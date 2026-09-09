# Paper Radar

Ogni mattina pesca i preprint nuovi da bioRxiv e PubMed, li fa valutare a Claude
e pubblica su una pagina web quelli che valgono la lettura. Serve a non dover
scorrere a mano le novità tutti i giorni.

**La pagina:** https://francesco-atlasbio.github.io/paper-radar/

Si aggiorna da sola alle 06:00 UTC. Non devi lanciare niente.

---

## Come funziona, in breve

```
bioRxiv + PubMed  →  filtro keyword  →  Claude dà un voto 1-10  →  docs/index.html
                     (grossolano)        (è lui che decide)
```

1. Scarica i paper degli ultimi `lookback_days` giorni da entrambe le fonti.
2. Tiene quelli che assomigliano alle tue keyword. È un filtro **largo apposta**:
   serve solo a ridurre il numero di chiamate API, non a decidere.
3. Manda titolo e abstract a Claude, che assegna un voto da 1 a 10, scrive una
   frase sul perché ti interessa, evidenzia i passaggi chiave dell'abstract e
   indica quali sezioni del paper leggere per prime.
4. Tiene i paper sopra `score_threshold` e genera la pagina.

Se una delle due fonti è irraggiungibile la run continua con l'altra. Se
nessun paper supera la soglia, la pagina mostra comunque i 3 migliori
disponibili, dicendo esplicitamente che è una giornata scarsa.

## Lanciarlo in locale

```bash
venv/bin/python main.py
```

Usa **sempre** `venv/bin/python`, mai `python3` da solo: le dipendenze stanno
nel virtualenv, non nel Python di sistema.

Se il `venv/` non c'è più (macchina nuova, cartella cancellata):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Rivalutare i paper già in cache

```bash
venv/bin/python main.py --rescore
```

Normalmente i paper già valutati vengono saltati, per non ripagare le chiamate
API. Con `--rescore` li rivaluta tutti. **Serve ogni volta che cambi le
keyword, il modello o il prompt**, altrimenti in pagina restano voti vecchi
dati con criteri diversi.

## Dove stanno i segreti

Due posti distinti, con gli stessi due valori:

| Dove | Cosa | Chi lo usa |
|---|---|---|
| `.env` nella cartella locale | `ANTHROPIC_API_KEY`, `PUBMED_EMAIL` | quando lanci a mano |
| GitHub → Settings → Secrets and variables → Actions | idem | la run automatica |

`.env` è in `.gitignore` e **non deve mai finire nel repo** — il repo è pubblico.
Il codice legge le variabili dall'ambiente: in locale le prende dal `.env`, in
CI dai secrets, senza differenze nel codice.

Se cambi la chiave API devi aggiornarla in **entrambi** i posti:

```bash
gh secret set ANTHROPIC_API_KEY --body "sk-ant-..."
```

## Dove vive la pagina

`docs/index.html`, generato da zero a ogni run. **Non modificarlo a mano**: la
run successiva sovrascrive tutto.

È versionato nel repo (non è in `.gitignore`) perché GitHub Pages serve proprio
quella cartella del branch `main`. Ogni run automatica lo committa se è
cambiato.

Per vederlo in locale senza aspettare il deploy: `open docs/index.html`.

## Cambiare le keyword

Tutto in `config.json`. **Non c'è niente da toccare nel codice.**

```json
{
  "keywords": [
    "clinical trial outcome prediction",
    "trial success prediction",
    "drug response prediction"
  ],
  "keyword_match_ratio": 0.75,
  "score_threshold": 7
}
```

Dopo averle cambiate, lancia `venv/bin/python main.py --rescore`.

### Le altre manopole

| Chiave | Cosa fa | Se la pagina è vuota |
|---|---|---|
| `keyword_match_ratio` | Quanta parte di una keyword deve comparire nel testo. `0.75` = 3 parole su 4 | **Abbassala** a `0.6`: rete più larga, più chiamate API |
| `score_threshold` | Voto minimo per finire in pagina | **Abbassala** a 5 o 6 |
| `lookback_days` | Quanti giorni indietro cercare a ogni run | Alzalo |
| `display_days` | Quanti giorni di paper mostrare in pagina | Alzalo |
| `fallback_count` | Quanti paper mostrare quando nessuno supera la soglia | `0` disattiva il ripiego |
| `max_papers_per_run` | Tetto alle chiamate API per run | Protezione dai costi, alzalo con cautela |
| `model` | Modello Claude usato | `claude-sonnet-5` di default |
| `pubmed_field` | `tiab` cerca solo in titolo e abstract | Metti `""` per cercare ovunque (molto più rumore) |

**Attenzione ai voti bassi.** Storicamente quasi nessun paper ha superato 7: le
keyword sono di nicchia e Claude è severo per come è scritto il prompt (`main.py`,
funzione `system_prompt`). Se pensi che i voti siano ingiustamente bassi, il
problema è lì, non nella configurazione.

## Se qualcosa si rompe

La run automatica che fallisce **apre una issue** nel repo, e GitHub te la
notifica via mail e app. Non devi controllare la tab Actions.

Se già c'è una issue di fallimento aperta, aggiunge un commento invece di
crearne una nuova. Quando hai risolto, chiudila a mano: è il segnale che il
prossimo fallimento ne aprirà una nuova.

Per indagare: `gh run list` e `gh run view --log-failed`.

## File del progetto

```
main.py                      tutto il programma, un unico file
config.json                  keyword e soglie — si modifica questo
requirements.txt             dipendenze con versioni fissate
.env                         segreti (NON versionato)
data/papers.json             cache dei paper già valutati (versionata, apposta)
docs/index.html              la pagina (generata)
.github/workflows/daily.yml  la run automatica
```

La cache è nel repo di proposito: la macchina di GitHub Actions parte vuota ogni
volta, quindi senza cache si ripagherebbe la valutazione degli stessi paper ogni
giorno. Si ripulisce da sola dopo `cache_retention_days` giorni.
