# Paper Radar

## Core problem
Nobody has time to scan new preprints daily. Relevant work gets missed.

## Who it's for
Me, and eventually the founders.

## What it does
1. Every morning, fetch new papers from bioRxiv and PubMed matching my keywords.
2. Send each title + abstract to Claude, which scores relevance 1-10 and writes
   a one-sentence "why you should care".
3. Keep anything scoring 7+, drop the rest.
4. Write results to a single HTML page, newest first.

## Keywords
[clinical trial outcome prediction, machine learning drug development]

## Success looks like
A web page I can open on my phone with 3-10 genuinely relevant papers,
updated daily, that I never have to trigger manually.

## What it must NOT do
- Not email anyone.
- Not store my API key in the code.
- Not crash the whole run if one source is down.
- Not require me to edit code to change keywords.