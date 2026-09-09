#!/usr/bin/env python3
"""Paper Radar - fetch new preprints, score them with Claude, publish a page.

Run:  venv/bin/python main.py
Config (keywords, model, thresholds) lives in config.json.
The API key is read from .env and never written anywhere.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
CACHE_PATH = os.path.join(ROOT, "data", "papers.json")
OUTPUT_PATH = os.path.join(ROOT, "docs", "index.html")

BIORXIV_API = "https://api.biorxiv.org/details/biorxiv"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
USER_AGENT = "paper-radar/1.0"
HTTP_TIMEOUT = 30


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("WARN: " + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# config & secrets
# --------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not cfg.get("keywords"):
        raise SystemExit("config.json has no keywords - nothing to search for.")
    return cfg


def load_secrets() -> Dict[str, str]:
    """Everything that must stay out of the public config file lives in .env."""
    load_dotenv(os.path.join(ROOT, ".env"))

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in .env as "
            "ANTHROPIC_API_KEY=sk-ant-..."
        )

    email = os.environ.get("PUBMED_EMAIL", "").strip()
    if not email:
        warn("PUBMED_EMAIL is not set in .env - NCBI asks for a contact address "
             "and may throttle anonymous callers.")

    return {"api_key": key, "pubmed_email": email}


# --------------------------------------------------------------------------
# paper identity & keyword matching
# --------------------------------------------------------------------------

def paper_key(paper: Dict[str, Any]) -> str:
    """Stable identity so a paper is only ever scored once."""
    if paper.get("doi"):
        return "doi:" + paper["doi"].strip().lower()
    if paper.get("pmid"):
        return "pmid:" + str(paper["pmid"]).strip()
    norm = re.sub(r"\W+", " ", paper.get("title", "")).strip().lower()
    return "title:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()


def keyword_match(paper: Dict[str, Any], keywords: List[str], ratio: float) -> bool:
    """True if enough of any one keyword's words appear in the title or abstract.

    This is a recall filter, not a precision filter - Claude's score threshold
    does the precision work downstream. Requiring the exact phrase, or even
    every word, finds almost nothing: over a sample week bioRxiv had 31 machine
    learning preprints and zero containing all four words of "machine learning
    drug development". Tune with keyword_match_ratio in config.json - lower
    casts a wider net and costs more scoring calls.
    """
    haystack = (paper.get("title", "") + " " + paper.get("abstract", "")).lower()
    for kw in keywords:
        words = [w for w in re.split(r"\W+", kw.lower()) if w]
        if not words:
            continue
        hits = sum(1 for w in words if w in haystack)
        if hits / len(words) >= ratio:
            return True
    return False


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def fetch_biorxiv(cfg: Dict[str, Any], start: str, end: str) -> List[Dict[str, Any]]:
    """bioRxiv has no server-side search, so page the window and filter locally."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    papers: List[Dict[str, Any]] = []
    cursor = 0
    total: Optional[int] = None

    while True:
        url = "{}/{}/{}/{}".format(BIORXIV_API, start, end, cursor)
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        collection = payload.get("collection") or []
        if total is None:
            total = int(payload.get("messages", [{}])[0].get("total", 0) or 0)
            log("  bioRxiv reports {} preprints in {}..{}".format(total, start, end))
        if not collection:
            break
        for item in collection:
            doi = (item.get("doi") or "").strip()
            papers.append({
                "source": "bioRxiv",
                "title": (item.get("title") or "").strip(),
                "abstract": (item.get("abstract") or "").strip(),
                "authors": (item.get("authors") or "").strip(),
                "date": (item.get("date") or "").strip(),
                "doi": doi,
                "pmid": "",
                "url": "https://doi.org/" + doi if doi else "",
            })
        cursor += len(collection)
        if total and cursor >= total:
            break
        time.sleep(0.5)

    return papers


def build_pubmed_term(cfg: Dict[str, Any]) -> str:
    field = (cfg.get("pubmed_field") or "").strip()
    suffix = "[{}]".format(field) if field else ""
    return " OR ".join("({}{})".format(kw, suffix) for kw in cfg["keywords"])


def fetch_pubmed(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    common = {"db": "pubmed", "tool": "paper-radar", "email": cfg.get("pubmed_email", "")}

    search = dict(common)
    search.update({
        "term": build_pubmed_term(cfg),
        "datetype": "edat",
        "reldate": int(cfg["lookback_days"]),
        "retmax": 200,
        "retmode": "json",
    })
    resp = session.get(EUTILS + "/esearch.fcgi", params=search, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    ids = resp.json().get("esearchresult", {}).get("idlist", [])
    log("  PubMed matched {} records in the last {} days".format(ids and len(ids) or 0,
                                                                 cfg["lookback_days"]))
    if not ids:
        return []

    time.sleep(0.4)
    fetch = dict(common)
    fetch.update({"id": ",".join(ids), "retmode": "xml"})
    resp = session.get(EUTILS + "/efetch.fcgi", params=fetch, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()

    papers: List[Dict[str, Any]] = []
    for article in ET.fromstring(resp.content).findall(".//PubmedArticle"):
        pmid = article.findtext(".//MedlineCitation/PMID", default="").strip()
        title = "".join(article.find(".//ArticleTitle").itertext()).strip() \
            if article.find(".//ArticleTitle") is not None else ""
        abstract = " ".join(
            "".join(node.itertext()).strip()
            for node in article.findall(".//Abstract/AbstractText")
        ).strip()
        doi = ""
        for eid in article.findall(".//ArticleId"):
            if eid.get("IdType") == "doi":
                doi = (eid.text or "").strip()
        authors = ", ".join(
            filter(None, [
                " ".join(filter(None, [a.findtext("ForeName"), a.findtext("LastName")]))
                for a in article.findall(".//AuthorList/Author")
            ])
        )
        papers.append({
            "source": "PubMed",
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "date": pubmed_date(article),
            "doi": doi,
            "pmid": pmid,
            "url": "https://pubmed.ncbi.nlm.nih.gov/{}/".format(pmid) if pmid else "",
        })
    return papers


def pubmed_date(article: ET.Element) -> str:
    """Entrez date if present, else the article's publication date."""
    for path in (".//PubMedPubDate[@PubStatus='entrez']", ".//PubMedPubDate[@PubStatus='pubmed']"):
        node = article.find(path)
        if node is not None:
            y, m, d = node.findtext("Year"), node.findtext("Month"), node.findtext("Day")
            if y:
                return "{}-{:02d}-{:02d}".format(int(y), int(m or 1), int(d or 1))
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_all(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run both sources; a source that fails is reported and skipped, not fatal."""
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=int(cfg["lookback_days"]))).isoformat()
    end = today.isoformat()

    papers: List[Dict[str, Any]] = []
    ok_sources = 0

    for name, fn in (("bioRxiv", lambda: fetch_biorxiv(cfg, start, end)),
                     ("PubMed", lambda: fetch_pubmed(cfg))):
        log("Fetching {}...".format(name))
        try:
            found = fn()
        except Exception as exc:  # one source down must not kill the run
            warn("{} fetch failed: {}".format(name, exc))
            continue
        ok_sources += 1
        ratio = float(cfg.get("keyword_match_ratio", 0.75))
        kept = [p for p in found if keyword_match(p, cfg["keywords"], ratio)]
        log("  {}: {} fetched, {} match keywords".format(name, len(found), len(kept)))
        papers.extend(kept)

    if ok_sources == 0:
        raise SystemExit("Both sources failed - leaving the existing page untouched.")
    return papers


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]},
        "why": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["quote", "note"],
                "additionalProperties": False,
            },
        },
        "read_first": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "why", "highlights", "read_first"],
    "additionalProperties": False,
}


def system_prompt(cfg: Dict[str, Any]) -> str:
    interests = "\n".join("- " + kw for kw in cfg["keywords"])
    return (
        "You triage new biomedical papers for a researcher whose interests are:\n"
        + interests
        + "\n\nGiven a title and abstract, rate relevance to those interests from 1 to 10. "
          "Be strict: 8-10 means directly on-topic and worth reading today, 5-7 means "
          "adjacent, 1-4 means it only shares vocabulary. Then write one sentence, "
          "under 30 words, saying why this researcher should care. Write the sentence "
          "for someone who has not read the abstract."
          "\n\nThen point them at what to read.\n"
          "highlights: up to 3 passages of the abstract that carry the most weight "
          "for these interests. Each quote must be copied VERBATIM from the abstract - "
          "an exact substring, no paraphrasing, no ellipses, no fixed typos - because "
          "the quotes are matched against the abstract text programmatically. Prefer "
          "one clause over a whole sentence. Give each a note of at most 8 words "
          "saying why it matters. Return an empty list if nothing stands out.\n"
          "read_first: up to 3 places in the full paper worth jumping to, named "
          "concretely - 'Methods: the external validation cohort', 'Table 2', "
          "'Limitations'. You are predicting these from the abstract alone, so only "
          "name a section when the abstract genuinely implies it is where the "
          "substance sits. Return an empty list otherwise."
    )


def score_paper(client: anthropic.Anthropic, cfg: Dict[str, Any],
                paper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = "Title: {}\n\nAbstract: {}".format(
        paper["title"], paper["abstract"] or "(no abstract provided)"
    )
    try:
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=512,
            system=system_prompt(cfg),
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
        )
    except anthropic.AuthenticationError:
        raise SystemExit("ANTHROPIC_API_KEY was rejected. Check .env.")
    except anthropic.NotFoundError:
        raise SystemExit(
            'Model "{}" was not found. Fix "model" in config.json.'.format(cfg["model"])
        )
    except anthropic.RateLimitError as exc:
        warn("rate limited, skipping this paper: {}".format(exc))
        return None
    except anthropic.APIError as exc:
        warn("scoring failed: {}".format(exc))
        return None

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        warn("unparseable score for: {}".format(paper["title"][:60]))
        return None


def score_new_papers(cfg: Dict[str, Any], api_key: str,
                     papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    client = anthropic.Anthropic(api_key=api_key)
    scored: List[Dict[str, Any]] = []
    total = len(papers)
    for i, paper in enumerate(papers, 1):
        log("  scoring {}/{}: {}".format(i, total, paper["title"][:70]))
        result = score_paper(client, cfg, paper)
        if result is None:
            continue
        enriched = dict(paper)
        enriched["score"] = int(result["score"])
        enriched["why"] = result["why"].strip()
        enriched["highlights"] = [
            {"quote": h.get("quote", "").strip(), "note": h.get("note", "").strip()}
            for h in (result.get("highlights") or [])
            if h.get("quote", "").strip()
        ]
        enriched["read_first"] = [
            s.strip() for s in (result.get("read_first") or []) if s.strip()
        ]
        enriched["scored_at"] = datetime.now(timezone.utc).isoformat()
        log("    -> {}/10".format(enriched["score"]))
        scored.append(enriched)
    return scored


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def load_cache() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return {p["key"]: p for p in json.load(fh)}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        warn("cache unreadable ({}), starting fresh".format(exc))
        return {}


def save_cache(cfg: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> None:
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=int(cfg["cache_retention_days"]))).isoformat()
    keep = [p for p in cache.values() if p.get("date", "") >= cutoff]
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)
    log("Cache holds {} scored papers (last {} days).".format(
        len(keep), cfg["cache_retention_days"]))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Radar</title>
<style>
  :root {{
    --bg: #f7f7f5; --card: #fff; --ink: #1a1a19; --muted: #6b6b66;
    --line: #e3e3df; --accent: #1c6b52;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #16171a; --card: #1e1f23; --ink: #e9e9e6; --muted: #9b9b95;
      --line: #303138; --accent: #6ec7a4;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 1.25rem 1rem 3rem; background: var(--bg); color: var(--ink);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 44rem; margin-inline: auto;
  }}
  header {{ margin-bottom: 1.5rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .2rem; letter-spacing: -.01em; }}
  .sub {{ color: var(--muted); font-size: .85rem; }}
  article {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 1rem 1.1rem; margin-bottom: .9rem;
  }}
  .meta {{
    display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
    font-size: .78rem; color: var(--muted); margin-bottom: .45rem;
  }}
  .score {{
    color: #fff; font-weight: 600; border-radius: 999px;
    padding: .1rem .55rem; font-size: .74rem;
  }}
  mark {{
    background: rgba(255, 213, 74, .38); color: inherit;
    padding: .05em .1em; border-radius: 3px;
  }}
  @media (prefers-color-scheme: dark) {{
    mark {{ background: rgba(255, 213, 74, .22); }}
  }}
  .notes {{ list-style: none; padding: 0; margin: 0 0 .6rem; }}
  .notes li {{
    font-size: .85rem; color: var(--muted); padding-left: .9rem;
    position: relative; margin-bottom: .18rem;
  }}
  .notes li::before {{
    content: ""; position: absolute; left: 0; top: .55em;
    width: .35rem; height: .35rem; border-radius: 50%; background: var(--accent);
  }}
  .readfirst {{
    font-size: .82rem; color: var(--muted); margin: .1rem 0 .6rem;
  }}
  .readfirst b {{ color: var(--ink); font-weight: 600; }}
  .tag {{
    display: inline-block; border: 1px solid var(--line); border-radius: 6px;
    padding: .05rem .4rem; margin: .12rem .25rem .12rem 0; color: var(--ink);
  }}
  h2 {{ font-size: 1.02rem; line-height: 1.35; margin: 0 0 .5rem; }}
  h2 a {{ color: var(--ink); text-decoration: none; }}
  h2 a:hover {{ text-decoration: underline; }}
  .why {{ margin: 0 0 .6rem; }}
  details summary {{
    cursor: pointer; color: var(--accent); font-size: .85rem;
    padding: .4rem 0; min-height: 44px; display: flex; align-items: center;
  }}
  details p {{ color: var(--muted); font-size: .9rem; margin: 0 0 .4rem; }}
  .empty {{ color: var(--muted); padding: 2rem 0; text-align: center; }}
  footer {{ color: var(--muted); font-size: .78rem; margin-top: 2rem;
            border-top: 1px solid var(--line); padding-top: .8rem; }}
</style>
</head>
<body>
<header>
  <h1>Paper Radar</h1>
  <div class="sub">{subtitle}</div>
</header>
{cards}
<footer>Built {built} &middot; {model} &middot; keywords: {keywords}</footer>
</body>
</html>
"""

CARD = """<article>
  <div class="meta"><span class="score" style="background:{color}">{score}/10</span><span>{source}</span><span>{date}</span></div>
  <h2><a href="{url}">{title}</a></h2>
  <p class="why">{why}</p>
{readfirst}{notes}  <details><summary>Abstract{marked}</summary><p>{abstract}</p><p>{authors}</p></details>
</article>"""


def score_color(score: int) -> str:
    """Dark red at 1, green at 10, amber through the middle."""
    t = (max(1, min(10, int(score))) - 1) / 9.0
    return "hsl({:.0f}, {:.0f}%, {:.0f}%)".format(138 * t, 68 - 10 * t, 31 + 7 * t)


def mark_highlights(abstract: str, highlights: List[Dict[str, str]], esc) -> str:
    """Escape the abstract, then wrap each verbatim quote in <mark>.

    Quotes come back from the model and are matched case-insensitively against
    the abstract. A quote that does not appear verbatim is skipped here - it
    still shows up as a note above the abstract, so nothing is silently lost.
    """
    if not abstract:
        return ""
    spans = []
    lowered = abstract.lower()
    for h in highlights:
        quote = h.get("quote", "")
        if len(quote) < 12:  # too short to locate reliably
            continue
        start = lowered.find(quote.lower())
        if start < 0:
            continue
        end = start + len(quote)
        if any(start < e and s < end for s, e in spans):  # overlapping
            continue
        spans.append((start, end))

    if not spans:
        return esc(abstract)

    out = []
    cursor = 0
    for start, end in sorted(spans):
        out.append(esc(abstract[cursor:start]))
        out.append("<mark>" + esc(abstract[start:end]) + "</mark>")
        cursor = end
    out.append(esc(abstract[cursor:]))
    return "".join(out)


def render(cfg: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> int:
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=int(cfg["display_days"]))).isoformat()
    recent = [p for p in cache.values() if p.get("date", "") >= cutoff]
    shown = [p for p in recent if p.get("score", 0) >= int(cfg["score_threshold"])]
    fallback = False

    if shown:
        # Normal view: everything over the bar, freshest first.
        shown.sort(key=lambda p: (p.get("date", ""), p.get("score", 0)), reverse=True)
    elif recent:
        # Nothing cleared the bar. Show the best of a thin day rather than an
        # empty page, ranked by score - on a day like this you care which is
        # least-bad, not which is newest.
        fallback = True
        recent.sort(key=lambda p: (p.get("score", 0), p.get("date", "")), reverse=True)
        shown = recent[:int(cfg.get("fallback_count", 3))]

    def esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=True)

    def build_card(p: Dict[str, Any]) -> str:
        highlights = p.get("highlights") or []
        read_first = p.get("read_first") or []

        notes = ""
        if highlights:
            notes = "  <ul class=\"notes\">\n" + "\n".join(
                "    <li>{}</li>".format(esc(h["note"] or h["quote"]))
                for h in highlights
            ) + "\n  </ul>\n"

        readfirst = ""
        if read_first:
            readfirst = '  <p class="readfirst"><b>Start with:</b> {}</p>\n'.format(
                "".join('<span class="tag">{}</span>'.format(esc(s)) for s in read_first)
            )

        body = mark_highlights(p.get("abstract", ""), highlights, esc) \
            or "No abstract available."
        n_marks = body.count("<mark>")
        marked = " &middot; {} passage{} flagged".format(
            n_marks, "" if n_marks == 1 else "s") if n_marks else ""

        return CARD.format(
            score=esc(p["score"]), color=score_color(p.get("score", 1)),
            source=esc(p["source"]), date=esc(p["date"]),
            url=esc(p["url"]) or "#", title=esc(p["title"]), why=esc(p["why"]),
            readfirst=readfirst, notes=notes, marked=marked,
            abstract=body, authors=esc(p["authors"]),
        )

    if shown:
        cards = "\n".join(build_card(p) for p in shown)
    else:
        cards = '<p class="empty">No papers matched your keywords in the last {} days.</p>'.format(
            int(cfg["display_days"]))

    plural = "" if len(shown) == 1 else "s"
    if fallback:
        subtitle = ("Nothing scored {}+ in the last {} days &mdash; "
                    "showing the {} best available".format(
                        int(cfg["score_threshold"]), int(cfg["display_days"]), len(shown)))
    else:
        subtitle = "{} paper{} scoring {}+ from the last {} days".format(
            len(shown), plural, int(cfg["score_threshold"]), int(cfg["display_days"]))

    page = PAGE.format(
        subtitle=subtitle,
        cards=cards,
        built=datetime.now().strftime("%Y-%m-%d %H:%M"),
        model=esc(cfg["model"]),
        keywords=esc(", ".join(cfg["keywords"])),
    )
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(shown)


# --------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    rescore = "--rescore" in argv
    unknown = [a for a in argv if a not in ("--rescore",)]
    if unknown:
        raise SystemExit("Unknown option(s): {}\nUsage: main.py [--rescore]".format(
            " ".join(unknown)))

    cfg = load_config()
    secrets = load_secrets()
    api_key = secrets["api_key"]
    # Kept in .env, not config.json, so the config can be committed publicly.
    cfg["pubmed_email"] = secrets["pubmed_email"]
    log("Paper Radar - {} keyword(s), model {}, keeping {}+".format(
        len(cfg["keywords"]), cfg["model"], cfg["score_threshold"]))

    candidates = fetch_all(cfg)
    log("{} candidate papers after keyword filtering.".format(len(candidates)))

    cache = load_cache()
    if rescore:
        log("--rescore: ignoring cached scores, re-scoring everything.")
    fresh: Dict[str, Dict[str, Any]] = {}
    for paper in candidates:
        key = paper_key(paper)
        if key in fresh or (key in cache and not rescore):
            continue
        paper["key"] = key
        fresh[key] = paper
    log("{} already scored in a previous run, {} to score.".format(
        len(candidates) - len(fresh), len(fresh)))

    to_score = list(fresh.values())
    cap = int(cfg["max_papers_per_run"])
    if len(to_score) > cap:
        log("Capping at {} papers this run (config: max_papers_per_run).".format(cap))
        to_score = to_score[:cap]

    if to_score:
        log("Scoring {} papers with {}...".format(len(to_score), cfg["model"]))
        for paper in score_new_papers(cfg, api_key, to_score):
            cache[paper["key"]] = paper
    else:
        log("Nothing new to score.")

    save_cache(cfg, cache)
    kept = render(cfg, cache)
    log("Done: {} paper(s) on the page -> {}".format(
        kept, os.path.relpath(OUTPUT_PATH, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
