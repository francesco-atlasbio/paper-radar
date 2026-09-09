# Regole del progetto

Paper Radar: scarica preprint da bioRxiv e PubMed, li fa valutare a Claude,
genera una pagina HTML statica. Girano in locale a mano e ogni giorno via
GitHub Actions.

## Python

Usa **`venv/bin/python`**, mai `python3` o `pip` di sistema. Le dipendenze
stanno nel virtualenv e sono fissate in `requirements.txt`.

Se aggiungi una dipendenza, installala nel venv e aggiorna `requirements.txt`
con la versione esatta risultante — niente pin inventati a memoria.

Il codice deve restare compatibile con **Python 3.9** (è quello di sistema su
questa macchina) anche se la CI gira su 3.12: niente `X | Y` nelle annotazioni,
niente `match`. Usa `typing.List` / `Optional`.

## Segreti

Solo in **`.env`**, letti dall'ambiente con `os.environ`. Mai valori veri nel
codice, in `config.json` o nei log.

Il repo è **pubblico**: prima di committare, controlla che non finisca dentro
niente di sensibile. `.env` è in `.gitignore` e ci resta.

In CI gli stessi valori arrivano dai GitHub Secrets. Il codice non distingue i
due casi: legge dall'ambiente e basta.

Quando mostri il contenuto di `.env` nel terminale, **maschera i valori** — il
terminale finisce nel log della conversazione.

## Configurazione

Keyword, soglie, modello e finestre temporali stanno in **`config.json`**.

Non mettere nel codice valori che l'utente potrebbe voler cambiare: niente
keyword hardcoded, niente nomi di modello, niente soglie. Se serve una manopola
nuova, va in `config.json` con un default sensato letto via `cfg.get(...)`, così
i config esistenti continuano a funzionare.

## Output

La pagina va in **`docs/index.html`**, rigenerata da zero a ogni run.
`docs/` è servita da GitHub Pages, quindi è versionata.

`data/papers.json` è la cache dei paper già valutati ed è versionata di
proposito: la macchina CI parte vuota, senza cache si ripaga la valutazione
degli stessi paper ogni giorno.

L'HTML è autonomo: CSS inline, niente JavaScript, niente CDN. Deve funzionare
da telefono e supportare tema chiaro e scuro. **Fai sempre `html.escape` su
titoli e abstract** — arrivano da API di terzi.

## Come lavorare

Il programma costa soldi a ogni run: ogni paper nuovo è una chiamata API. Non
lanciarlo a ripetizione per provare cose. Per verificare il rendering usa dati
sintetici passati a `render()`; per rivalutare la cache esiste `--rescore`.

Il programma stampa cosa sta facendo mentre gira: mantieni questa abitudine
quando aggiungi step.

Nessuna fonte deve poter far fallire l'intera run: se bioRxiv o PubMed sono
giù, logga un warning e continua con l'altra.

Prima di dire che qualcosa funziona, **verificalo davvero** — esegui il codice,
controlla l'output. Niente conclusioni basate solo sulla lettura del codice.
