#!/usr/bin/env python3
"""Morning Briefing - Grounded edition.

Pipeline:
  1. Fetch RSS feeds (titles, descriptions, links, pubDates).
  2. Filter to last N hours, dedup near-duplicate stories by title similarity.
  3. Selector (Haiku): choose CANDIDATE_COUNT items to investigate.
  4. Fetcher: pull full article text for each candidate via Jina Reader
     (https://r.jina.ai/) with raw-HTML fallback.
  5. Drafter (Sonnet): write briefing using ONLY fetched article text.
     Model must return source_snippets backing every claim.
  6. Validator (programmatic): every number and quoted span in the briefing
     must appear in the cited source text. Sentences failing this are dropped.
  7. Verifier (Sonnet): independent fact-checking pass; flagged sentences
     are stripped from the briefing.
  8. Publish to Telegraph with per-story "Source: Publisher" links.
  9. Push via ntfy.

If too few stories survive grounding, the script ships a headlines-only
briefing rather than publish hallucinated prose.
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import requests

# -- Config ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "con-hillier-morning-briefing")
DRY_RUN = bool(os.environ.get("BRIEFING_DRY_RUN"))

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
ARTICLE_FETCH_TIMEOUT = 20
JINA_READER_PREFIX = "https://r.jina.ai/"

SELECTOR_MODEL = "claude-haiku-4-5-20251001"
DRAFTER_MODEL = "claude-sonnet-4-6"
VERIFIER_MODEL = "claude-sonnet-4-6"

CANDIDATE_COUNT = 8           # How many RSS items to fetch articles for
TARGET_STORY_COUNT = 5        # Final featured-story count
MIN_STORY_COUNT = 3           # Below this -> downgrade to headlines-only
ARTICLE_MAX_CHARS = 6000      # Truncate each fetched article
FRESHNESS_HOURS = 30          # Keep items dated within this window
MAX_VALIDATION_DROPS = 2      # Max sentences validator can drop before rejecting a story

DEDUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_briefing.json")
DEDUP_DAYS = 3

RSS_FEEDS = [
    # -- World & Geopolitics --
    {"url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "World", "publisher": "BBC"},
    {"url": "https://www.theguardian.com/world/rss", "category": "World", "publisher": "The Guardian"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "World", "publisher": "Al Jazeera"},
    {"url": "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com&hl=en-GB&gl=GB&ceid=GB:en", "category": "World", "publisher": "Reuters"},
    # -- Business & Markets --
    {"url": "http://feeds.bbci.co.uk/news/business/rss.xml", "category": "Business", "publisher": "BBC"},
    {"url": "https://www.ft.com/rss/home", "category": "Business", "publisher": "Financial Times"},
    {"url": "https://www.theguardian.com/uk/business/rss", "category": "Business", "publisher": "The Guardian"},
    {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "Markets", "publisher": "CNBC"},
    # -- Technology & AI --
    {"url": "http://feeds.bbci.co.uk/news/technology/rss.xml", "category": "Technology", "publisher": "BBC"},
    {"url": "https://www.theguardian.com/uk/technology/rss", "category": "Technology", "publisher": "The Guardian"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "AI", "publisher": "TechCrunch"},
    # -- Closer to home --
    {"url": "http://feeds.bbci.co.uk/news/scotland/rss.xml", "category": "Scotland", "publisher": "BBC"},
    {"url": "https://www.theguardian.com/uk-news/rss", "category": "UK", "publisher": "The Guardian"},
]


# -- Logging -----------------------------------------------------------------
def log(message, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {message}", flush=True)


# -- Text Sanitizer ----------------------------------------------------------
def sanitize_text(text):
    """Strip non-ASCII characters, replacing common ones with ASCII equivalents."""
    if not text:
        return text
    text = unicodedata.normalize("NFKD", text)
    for ch in "‘’‚‛":
        text = text.replace(ch, "'")
    for ch in "“”„‟":
        text = text.replace(ch, '"')
    text = text.replace("–", "-")    # en dash
    text = text.replace("—", " - ")  # em dash
    text = text.replace("―", " - ")  # horizontal bar
    text = text.replace("…", "...")
    text = text.replace("•", "-")
    text = text.replace("‣", "-")
    text = text.replace(" ", " ")
    text = text.replace("«", '"').replace("»", '"')
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = "".join(ch if 32 <= ord(ch) <= 126 else "" for ch in text)
    return text


def parse_markdown_bold(text):
    """Convert **bold** markdown into Telegraph children list."""
    parts = re.split(r"\*\*(.+?)\*\*", text)
    if len(parts) == 1:
        return [text]
    children = []
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 1:
            children.append({"tag": "strong", "children": [part]})
        else:
            children.append(part)
    return children


# -- Dedup file --------------------------------------------------------------
def _safe_parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def load_previous_headlines():
    try:
        if not os.path.exists(DEDUP_FILE):
            log("No dedup file found (first run)")
            return []
        with open(DEDUP_FILE, "r") as f:
            data = json.load(f)
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
        recent = [e for e in data if _safe_parse_iso(e.get("date")) and _safe_parse_iso(e["date"]) > cutoff]
        headlines = []
        for entry in recent:
            headlines.extend(entry.get("headlines", []))
        log(f"Loaded {len(headlines)} previous headlines from {len(recent)} day(s)")
        return headlines
    except Exception as e:
        log(f"Failed to load dedup file: {e}", "WARN")
        return []


def save_briefing_headlines(briefing):
    try:
        existing = []
        if os.path.exists(DEDUP_FILE):
            with open(DEDUP_FILE, "r") as f:
                existing = json.load(f)
        headlines = []
        for s in briefing.get("stories", []):
            body_preview = (s.get("body", "") or "")[:120].strip()
            headlines.append(f"{s['headline']} -- {body_preview}")
        cth = briefing.get("closer_to_home")
        if cth:
            cth_items = cth if isinstance(cth, list) else [cth]
            for item in cth_items:
                body_preview = (item.get("body", "") or "")[:120].strip()
                headlines.append(f"{item['headline']} -- {body_preview}")
        headlines.extend(briefing.get("quick_hits", []) or [])
        existing.append({
            "date": datetime.now(timezone.utc).isoformat(),
            "headlines": headlines,
        })
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
        existing = [e for e in existing if _safe_parse_iso(e.get("date")) and _safe_parse_iso(e["date"]) > cutoff]
        with open(DEDUP_FILE, "w") as f:
            json.dump(existing, f, indent=2)
        log(f"Saved {len(headlines)} headlines to dedup file ({len(existing)} day(s) retained)")
    except Exception as e:
        log(f"Failed to save dedup file: {e}", "WARN")


# -- RSS fetch + freshness + dedup ------------------------------------------
def fetch_feed(url, category, publisher, max_items=6):
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            if not title:
                continue
            desc = re.sub(r"<[^>]+>", "", entry.get("summary", entry.get("description", "")))
            desc = html.unescape(desc).strip()
            if len(desc) > 400:
                desc = desc[:397] + "..."
            link = entry.get("link", "").strip()
            pub_raw = entry.get("published") or entry.get("updated") or ""
            pub_dt = None
            if pub_raw:
                try:
                    pub_dt = parsedate_to_datetime(pub_raw)
                    if pub_dt and pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = None
            items.append({
                "title": title,
                "description": desc,
                "link": link,
                "category": category,
                "publisher": publisher,
                "pubDate": pub_dt,
            })
        log(f"Fetched {len(items)} items from {publisher} {category}")
        return items
    except Exception as e:
        log(f"Failed to fetch {category} feed: {e}", "WARN")
        return []


def fetch_all_news():
    all_news = []
    for feed in RSS_FEEDS:
        all_news.extend(fetch_feed(feed["url"], feed["category"], feed["publisher"]))
    log(f"Total raw news items fetched: {len(all_news)}")
    return all_news


def filter_fresh(items, hours=FRESHNESS_HOURS):
    """Keep items with pubDate within the window. Items missing pubDate are kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    fresh, undated = [], []
    for it in items:
        if it["pubDate"]:
            if it["pubDate"] > cutoff:
                fresh.append(it)
        else:
            undated.append(it)
    log(f"Freshness filter ({hours}h): kept {len(fresh)} dated + {len(undated)} undated of {len(items)}")
    return fresh + undated


_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "with", "as",
    "is", "are", "was", "were", "by", "at", "from", "after", "before",
    "over", "under", "amid", "into", "out", "up", "down", "off", "his",
    "her", "its", "their", "this", "that", "than", "but", "or",
}


def _title_tokens(title):
    tokens = re.findall(r"[A-Za-z0-9]+", title.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


def prededuplicate(items, threshold=0.5):
    """Drop near-duplicate stories across feeds by title-token Jaccard similarity."""
    kept, kept_tokens, dropped = [], [], 0
    for it in items:
        toks = _title_tokens(it["title"])
        if not toks:
            kept.append(it); kept_tokens.append(toks); continue
        dup = False
        for prev in kept_tokens:
            if not prev:
                continue
            union = len(toks | prev)
            if union and (len(toks & prev) / union) >= threshold:
                dup = True; break
        if dup:
            dropped += 1
        else:
            kept.append(it); kept_tokens.append(toks)
    log(f"Pre-dedup: dropped {dropped}, kept {len(kept)}")
    return kept


# -- Article fetcher (Jina Reader + raw HTML fallback) ----------------------
_article_cache = {}


def fetch_article(url):
    """Fetch full article text. Tries Jina Reader, falls back to raw HTML."""
    if not url:
        return ""
    if url in _article_cache:
        return _article_cache[url]
    text = _fetch_via_jina(url)
    if not text or len(text) < 250:
        text = _fetch_via_raw(url)
    if text:
        text = text.strip()
        if len(text) > ARTICLE_MAX_CHARS:
            text = text[:ARTICLE_MAX_CHARS]
    _article_cache[url] = text or ""
    return _article_cache[url]


def _fetch_via_jina(url):
    try:
        resp = requests.get(
            JINA_READER_PREFIX + url,
            headers={"Accept": "text/plain", "X-Return-Format": "text"},
            timeout=ARTICLE_FETCH_TIMEOUT,
        )
        if resp.status_code == 200 and resp.text:
            return _clean_article_text(resp.text)
    except Exception as e:
        log(f"Jina fetch failed for {url[:60]}: {e}", "WARN")
    return ""


def _fetch_via_raw(url):
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; morning-briefing/1.0)"},
            timeout=ARTICLE_FETCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        h = resp.text
        h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S | re.I)
        h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S | re.I)
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", h, flags=re.S | re.I)
        parts = []
        for p in paragraphs:
            p = re.sub(r"<[^>]+>", "", p)
            p = html.unescape(p).strip()
            if len(p) > 40:
                parts.append(p)
        return _clean_article_text("\n\n".join(parts))
    except Exception as e:
        log(f"Raw fetch failed for {url[:60]}: {e}", "WARN")
        return ""


def _clean_article_text(text):
    text = html.unescape(text)
    skip = ("cookie", "subscribe", "newsletter", "advertisement", "follow us on",
            "sign up to", "sign up for")
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        low = ln.lower()
        if any(t in low for t in skip):
            continue
        out.append(ln)
    return "\n".join(out)


# -- Claude API call --------------------------------------------------------
def call_claude(model, system, user, max_tokens=4096):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                json=body, headers=headers, timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as e:
            log(f"Claude ({model}) attempt {attempt} failed: {e}", "WARN")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(attempt * 8)


def _extract_json(text):
    text = re.sub(r"^\s*```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


# -- Stage 1: Selector -------------------------------------------------------
def select_candidates(items, previous_headlines):
    """Pick CANDIDATE_COUNT items for full-article fetching. Returns indices."""
    if not items:
        return []
    digest_lines = []
    for i, it in enumerate(items):
        desc = it["description"][:160] if it["description"] else ""
        digest_lines.append(f"{i}. [{it['category']}/{it['publisher']}] {it['title']}\n    {desc}")
    digest_text = "\n".join(digest_lines)

    dedup_block = ""
    if previous_headlines:
        dedup_block = "\nRecently-covered topics (skip duplicates and stale follow-ups):\n"
        for h in previous_headlines[:30]:
            dedup_block += f"- {h}\n"

    user = (
        f"Today's RSS pool. Select the {CANDIDATE_COUNT} most important and news-worthy items "
        f"for a morning briefing. Aim for a mix: ~3 World/Geopolitics, ~2-3 Business/Markets, "
        f"~1-2 Technology/AI, plus 1 UK/Scotland if anything notable.\n\n"
        f"Prefer concrete events over opinion, analysis, or evergreen features.\n"
        f"{dedup_block}\n"
        f"=== RSS POOL ===\n{digest_text}\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{ "candidates": [<index>, <index>, ...] }}  // exactly {CANDIDATE_COUNT} indices in priority order'
    )
    system = "You are a sharp news editor. You output only valid JSON. No commentary."

    raw = call_claude(SELECTOR_MODEL, system, user, max_tokens=400)
    parsed = _extract_json(raw)
    idxs = []
    for v in parsed.get("candidates", []):
        try:
            i = int(v)
            if 0 <= i < len(items):
                idxs.append(i)
        except (TypeError, ValueError):
            continue
    # Dedup preserving order
    seen, ordered = set(), []
    for i in idxs:
        if i not in seen:
            seen.add(i); ordered.append(i)
    log(f"Selector chose {len(ordered)} candidates: {ordered}")
    return ordered


# -- Stage 2: Fetch full article text for selected candidates ---------------
def fetch_articles_for(items):
    out = []
    for it in items:
        text = fetch_article(it["link"])
        if text and len(text) >= 300:
            out.append({**it, "article_text": text})
            log(f"Fetched article ({len(text)} chars): {it['title'][:60]}")
        else:
            log(f"Skip (no article text, got {len(text)} chars): {it['title'][:60]}", "WARN")
    return out


# -- Stage 3: Draft briefing (grounded) -------------------------------------
def draft_briefing(articles, previous_headlines):
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")

    src_blocks = []
    for i, a in enumerate(articles):
        src_blocks.append(
            f"=== SOURCE {i} ===\n"
            f"Publisher: {a['publisher']} ({a['category']})\n"
            f"Headline: {a['title']}\n"
            f"Article text:\n{a['article_text']}\n"
        )
    sources_text = "\n".join(src_blocks)

    dedup_block = ""
    if previous_headlines:
        dedup_block = "\nRecently covered (do not repeat unless materially new):\n"
        for h in previous_headlines[:25]:
            dedup_block += f"- {h}\n"

    system = f"""You are writing the daily morning briefing for Con in Glasgow. Today is {today}.

ABSOLUTE GROUNDING RULES (non-negotiable):
- You may ONLY state facts that appear in the SOURCE articles provided.
- Every number, percentage, currency figure, date, name, place, quote, and statistic in your output MUST appear in at least one source.
- If a source lacks material to write a full paragraph, write FEWER sentences for that story, or omit the story entirely. Do NOT fill gaps from memory.
- Do NOT contextualise numbers (e.g. "highest since 2022", "up 12% in a week") unless that exact comparison appears in the source.
- Do NOT add background, causation, or "what happens next" unless the source explicitly says so.
- Do NOT invent quotes. Only quote text that appears word-for-word in the source.
- A short, accurate briefing is BETTER than a long, confident, wrong one.

OUTPUT FORMAT - For each story:
- headline: short, max 8 words.
- body: 2-4 sentences of WHAT HAPPENED, strictly from the source. Use **bold** for key facts only when those facts are in the source.
- why_it_matters: 1-2 sentences of implication. ONLY include if the source itself states the implication, or it is a direct logical consequence (e.g. "rate hike" -> "borrowing costs rise"). If unsure, set to null.
- source_index: integer matching one of the SOURCE blocks.
- source_snippets: 2-4 verbatim sentences from the source supporting your story. These must appear in the source text EXACTLY as written.

ENCODING: ASCII only. Straight quotes. Hyphens not dashes. No accented characters.

Return ONLY valid JSON. No markdown fences, no commentary."""

    user = f"""Source articles below. Use ONLY their text.
{dedup_block}
{sources_text}

Write up to {TARGET_STORY_COUNT} stories drawn from the sources above. Also produce:
- closer_to_home: ONE UK or Scotland story if a source covers it (object with headline, body, source_index, source_snippets). Otherwise null.
- quick_hits: Up to 4 one-sentence summaries of OTHER newsworthy sources you did not feature. Each must faithfully summarise a specific source. Format each entry as {{"text": "the one-line summary", "source_index": N}}.
- bottom_line: ONE sentence synthesising the day's mood. No new facts.

Return ONLY valid JSON in this shape:
{{
  "stories": [
    {{
      "headline": "...",
      "body": "...",
      "why_it_matters": "..." or null,
      "source_index": 0,
      "source_snippets": ["verbatim sentence", "verbatim sentence"]
    }}
  ],
  "closer_to_home": {{ "headline":"...", "body":"...", "source_index":N, "source_snippets":[...] }} or null,
  "quick_hits": [ {{"text":"...", "source_index": N}} ],
  "bottom_line": "One sentence."
}}"""

    raw = call_claude(DRAFTER_MODEL, system, user, max_tokens=4096)
    briefing = _extract_json(raw)
    log(f"Drafter produced {len(briefing.get('stories', []))} stories")
    return briefing


# -- Stage 4: Programmatic validator ----------------------------------------
# Matches digit-bearing tokens including currency prefixes and unit suffixes.
_NUMBER_RE = re.compile(
    # Alternation order matters: Python re matches alternatives left-to-right,
    # so longer suffixes (`pounds`) must come before shorter ones (`p`) or the
    # short one wins and `pounds` is split as `p` + `ounds`.
    r"[\$£€]?\d[\d,]*(?:\.\d+)?\s?(?:percent|pounds|dollars|euros|pence|bn|%|p|m|k)?",
    re.I,
)
_QUOTE_RE = re.compile(r'"([^"]{4,})"')


def _normalize(s):
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(",", "")
    return s.strip()


def _extract_numbers(text):
    out = []
    for m in _NUMBER_RE.finditer(text):
        tok = m.group(0).strip()
        # Skip bare single digits (e.g. "1" from list items).
        digits = re.sub(r"\D", "", tok)
        if len(digits) >= 2 or (digits and any(c in tok for c in "$£€%")):
            out.append(tok)
    return out


def _extract_quotes(text):
    return [m.group(1) for m in _QUOTE_RE.finditer(text)]


def _split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _number_supported(token, source_norm):
    # Strip non-digit-and-dot to get the bare number, then check substring.
    bare = re.sub(r"[^\d.]", "", token)
    if not bare:
        return True
    return bare in source_norm


def _quote_supported(quote_text, source_norm):
    return _normalize(quote_text) in source_norm


def validate_story(story, sources):
    """Drop sentences whose numbers/quotes aren't in source. Returns (story_or_None, warnings)."""
    warnings = []
    src_idx = story.get("source_index")
    if not isinstance(src_idx, int) or not (0 <= src_idx < len(sources)):
        return None, [f"invalid source_index: {src_idx}"]
    source_norm = _normalize(sources[src_idx]["article_text"])

    def clean(field_text):
        if not field_text:
            return field_text, 0
        kept, drops = [], 0
        for sent in _split_sentences(field_text):
            unsupported = []
            for n in _extract_numbers(sent):
                if not _number_supported(n, source_norm):
                    unsupported.append(f"number `{n}`")
            for q in _extract_quotes(sent):
                if not _quote_supported(q, source_norm):
                    unsupported.append(f'quote "{q[:40]}..."')
            if unsupported:
                warnings.append(f"drop [{', '.join(unsupported)}]: {sent[:90]}")
                drops += 1
            else:
                kept.append(sent)
        return " ".join(kept), drops

    body, body_drops = clean(story.get("body", ""))
    wim, wim_drops = clean(story.get("why_it_matters") or "")

    if body_drops + wim_drops > MAX_VALIDATION_DROPS:
        warnings.append(f"too many drops ({body_drops + wim_drops}); rejecting story")
        return None, warnings
    if not body or len(body) < 40:
        warnings.append("body too thin after validation; rejecting story")
        return None, warnings

    return {**story, "body": body, "why_it_matters": wim or None}, warnings


def validate_briefing(briefing, sources):
    stories, warns_all = [], []
    for s in briefing.get("stories", []):
        cleaned, warns = validate_story(s, sources)
        warns_all.extend(warns)
        if cleaned:
            stories.append(cleaned)

    cth_clean = None
    if briefing.get("closer_to_home"):
        cleaned, warns = validate_story(briefing["closer_to_home"], sources)
        warns_all.extend(warns)
        cth_clean = cleaned

    hits = []
    for hit in (briefing.get("quick_hits") or []):
        text = hit.get("text") if isinstance(hit, dict) else str(hit)
        src_idx = hit.get("source_index") if isinstance(hit, dict) else None
        if isinstance(src_idx, int) and 0 <= src_idx < len(sources):
            src_norm = _normalize(sources[src_idx]["article_text"])
            bad = (any(not _number_supported(n, src_norm) for n in _extract_numbers(text)) or
                   any(not _quote_supported(q, src_norm) for q in _extract_quotes(text)))
            if bad:
                warns_all.append(f"drop quick hit: {text[:80]}")
                continue
        hits.append(text)

    if warns_all:
        log(f"Validator findings ({len(warns_all)}):", "WARN")
        for w in warns_all:
            log(f"  - {w}", "WARN")

    return {
        "stories": stories,
        "closer_to_home": cth_clean,
        "quick_hits": hits,
        "bottom_line": briefing.get("bottom_line", ""),
    }


# -- Stage 5: Verifier pass --------------------------------------------------
def verify_briefing(briefing, sources):
    if not briefing.get("stories"):
        return briefing

    sources_text = "\n\n".join(
        f"=== SOURCE {i} ({a['publisher']}) ===\n{a['article_text']}"
        for i, a in enumerate(sources)
    )

    draft_lines = []
    for i, s in enumerate(briefing["stories"]):
        draft_lines.append(f"STORY {i} (source_index={s.get('source_index')}):")
        draft_lines.append(f"Headline: {s['headline']}")
        draft_lines.append(f"Body: {s.get('body','')}")
        if s.get("why_it_matters"):
            draft_lines.append(f"Why it matters: {s['why_it_matters']}")
        draft_lines.append("")
    draft_str = "\n".join(draft_lines)

    system = (
        "You are a strict fact-checker. You compare a draft briefing against source articles "
        "and output only valid JSON. For each sentence in each story, decide whether every "
        "claim in that sentence is directly supported by the cited source.\n\n"
        "A sentence is UNSUPPORTED if it contains a number, name, date, place, or quote not "
        "in the source; if it makes a causal/contextual claim (e.g. 'highest since X', "
        "'driven by Y') the source does not state; or if it paraphrases the source in a way "
        "that changes meaning. When in doubt, mark unsupported."
    )

    user = f"""SOURCES:

{sources_text}

DRAFT BRIEFING:

{draft_str}

Return ONLY JSON:
{{
  "unsupported": [
    {{"story_index": 0, "sentence": "exact unsupported sentence from the draft", "reason": "short"}}
  ]
}}"""

    try:
        raw = call_claude(VERIFIER_MODEL, system, user, max_tokens=2048)
        report = _extract_json(raw)
    except Exception as e:
        log(f"Verifier failed (passing post-validator draft through): {e}", "WARN")
        return briefing

    flagged = report.get("unsupported", []) or []
    if not flagged:
        log("Verifier: 0 unsupported claims")
        return briefing

    log(f"Verifier flagged {len(flagged)} unsupported claims:", "WARN")
    for f in flagged:
        log(f"  - story {f.get('story_index')}: {str(f.get('sentence',''))[:90]} ({f.get('reason','')})", "WARN")

    cleaned = []
    for i, story in enumerate(briefing["stories"]):
        to_drop = [
            f.get("sentence", "").strip()
            for f in flagged
            if f.get("story_index") == i and f.get("sentence")
        ]

        def strip(field_text):
            if not field_text:
                return field_text
            kept = []
            for s in _split_sentences(field_text):
                s_strip = s.strip()
                drop_it = any(
                    d == s_strip or (d and (d in s_strip or s_strip in d))
                    for d in to_drop
                )
                if not drop_it:
                    kept.append(s)
            return " ".join(kept)

        new_body = strip(story.get("body", ""))
        new_wim = strip(story.get("why_it_matters") or "")
        if not new_body or len(new_body) < 40:
            log(f"  story {i} body too thin after verifier; dropping story", "WARN")
            continue
        cleaned.append({**story, "body": new_body, "why_it_matters": new_wim or None})

    return {**briefing, "stories": cleaned}


# -- Telegraph publish (with source links) ----------------------------------
def publish_to_telegraph(briefing, sources):
    now = datetime.now(timezone.utc)
    today = now.strftime("%A %d %B %Y")
    date_suffix = now.strftime("%d%b%H%M")

    nodes = []
    nodes.append({"tag": "p", "children": [{"tag": "em", "children": [f"{today} | 4-minute read"]}]})

    for i, story in enumerate(briefing["stories"], 1):
        nodes.append({"tag": "h3", "children": [sanitize_text(f"{i}. {story['headline']}")]})
        body_text = sanitize_text(story.get("body", ""))
        for p in [p.strip() for p in re.split(r"\n+", body_text) if p.strip()]:
            nodes.append({"tag": "p", "children": parse_markdown_bold(p)})
        wim = story.get("why_it_matters")
        if wim:
            wim_text = sanitize_text(wim)
            wim_children = [{"tag": "strong", "children": ["Why it matters: "]}]
            wim_children.extend(parse_markdown_bold(wim_text))
            nodes.append({"tag": "p", "children": wim_children})
        src_idx = story.get("source_index")
        if isinstance(src_idx, int) and 0 <= src_idx < len(sources):
            src = sources[src_idx]
            nodes.append({"tag": "p", "children": [{"tag": "em", "children": [
                "Source: ",
                {"tag": "a", "attrs": {"href": src["link"]}, "children": [sanitize_text(src["publisher"])]},
            ]}]})

    cth = briefing.get("closer_to_home")
    if cth:
        nodes.append({"tag": "h3", "children": [sanitize_text("Closer to Home")]})
        nodes.append({"tag": "p", "children": [{"tag": "strong", "children": [sanitize_text(cth["headline"])]}]})
        for p in [p.strip() for p in re.split(r"\n+", sanitize_text(cth.get("body", ""))) if p.strip()]:
            nodes.append({"tag": "p", "children": parse_markdown_bold(p)})
        src_idx = cth.get("source_index")
        if isinstance(src_idx, int) and 0 <= src_idx < len(sources):
            src = sources[src_idx]
            nodes.append({"tag": "p", "children": [{"tag": "em", "children": [
                "Source: ",
                {"tag": "a", "attrs": {"href": src["link"]}, "children": [sanitize_text(src["publisher"])]},
            ]}]})

    if briefing.get("quick_hits"):
        nodes.append({"tag": "h4", "children": ["Quick Hits"]})
        for hit in briefing["quick_hits"]:
            nodes.append({"tag": "p", "children": parse_markdown_bold(f"- {sanitize_text(hit)}")})

    if briefing.get("bottom_line"):
        nodes.append({"tag": "h3", "children": ["Bottom Line"]})
        nodes.append({"tag": "p", "children": parse_markdown_bold(sanitize_text(briefing["bottom_line"]))})

    nodes.append({"tag": "p", "children": [" "]})
    nodes.append({"tag": "p", "children": [{"tag": "em", "children": ["Your morning briefing, delivered daily."]}]})

    acct = requests.get(
        f"https://api.telegra.ph/createAccount?short_name=ConBrief{date_suffix}&author_name=Morning%20Briefing",
        timeout=REQUEST_TIMEOUT,
    ).json()
    if not acct.get("ok"):
        raise RuntimeError(f"Telegraph account creation failed: {acct}")
    token = acct["result"]["access_token"]

    page = requests.post(
        "https://api.telegra.ph/createPage",
        json={
            "access_token": token,
            "title": sanitize_text(f"Morning Briefing - {today}"),
            "author_name": "Con's Daily Briefing",
            "content": nodes,
            "return_content": False,
        },
        timeout=REQUEST_TIMEOUT,
    ).json()
    if not page.get("ok"):
        raise RuntimeError(f"Telegraph page creation failed: {page}")

    url = page["result"]["url"]
    log(f"Telegraph page published: {url}")
    return url


# -- ntfy push --------------------------------------------------------------
def send_ntfy(url, briefing):
    date_short = datetime.now(timezone.utc).strftime("%a %d %b")
    body_lines = []
    for i, story in enumerate(briefing["stories"], 1):
        body_lines.append(f"**{i}.** {story['headline']}")
    cth = briefing.get("closer_to_home")
    if cth:
        body_lines.append(f"**UK:** {cth['headline']}")
    body_lines.append("")
    body_lines.append(f"[Read the full briefing here]({url})")
    body = "\n".join(body_lines)
    resp = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"Morning Briefing - {date_short}",
            "Tags": "newspaper",
            "Actions": f"view, Read full briefing, {url}, clear=true",
            "Markdown": "yes",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    log("ntfy notification sent")


# -- Headlines-only fallback ------------------------------------------------
def headlines_only_briefing(items):
    log("Falling back to headlines-only briefing (insufficient grounded content)", "WARN")
    stories = []
    for it in items[:5]:
        stories.append({
            "headline": it["title"],
            "body": it["description"] or it["title"],
            "why_it_matters": None,
            "source_index": None,
        })
    return {
        "stories": stories,
        "closer_to_home": None,
        "quick_hits": [it["title"] for it in items[5:10]],
        "bottom_line": "Limited briefing today - grounded content was unavailable.",
    }


# -- Main -------------------------------------------------------------------
def main():
    log("=========================================")
    log("=== Morning briefing started ===")

    if not ANTHROPIC_API_KEY:
        log("FATAL: ANTHROPIC_API_KEY not set", "ERROR")
        sys.exit(1)

    log("Fetching RSS feeds...")
    raw = fetch_all_news()
    if not raw:
        log("No RSS items at all. Aborting.", "ERROR")
        sys.exit(1)

    fresh = filter_fresh(raw)
    deduped = prededuplicate(fresh)
    previous = load_previous_headlines()

    # Stage 1: select
    try:
        idxs = select_candidates(deduped, previous)
    except Exception as e:
        log(f"Selector failed: {e}; using first {CANDIDATE_COUNT} items as fallback", "WARN")
        idxs = list(range(min(CANDIDATE_COUNT, len(deduped))))

    candidates = [deduped[i] for i in idxs] if idxs else deduped[:CANDIDATE_COUNT]

    # Stage 2: fetch articles
    log("Fetching article bodies for candidates...")
    articles = fetch_articles_for(candidates)

    grounded = False
    if len(articles) >= MIN_STORY_COUNT:
        try:
            log("Drafting briefing (grounded)...")
            draft = draft_briefing(articles, previous)
            log("Validating draft (programmatic)...")
            validated = validate_briefing(draft, articles)
            log(f"After validator: {len(validated['stories'])} stories")
            try:
                log("Running verifier pass...")
                briefing = verify_briefing(validated, articles)
                log(f"After verifier: {len(briefing['stories'])} stories")
            except Exception as e:
                log(f"Verifier crashed: {e}; using post-validator draft", "WARN")
                briefing = validated

            if len(briefing.get("stories", [])) >= MIN_STORY_COUNT:
                grounded = True
            else:
                log("Too few stories survived grounding pipeline", "WARN")
        except Exception as e:
            log(f"Grounded pipeline failed: {e}", "ERROR")

    if not grounded:
        briefing = headlines_only_briefing(deduped)
        articles = []

    if DRY_RUN:
        log("DRY RUN - skipping publish")
        print(json.dumps(briefing, indent=2, default=str))
        return

    log("Publishing to Telegraph...")
    url = publish_to_telegraph(briefing, articles)

    log("Sending push notification...")
    send_ntfy(url, briefing)

    save_briefing_headlines(briefing)

    log("=== Morning briefing complete ===")
    log("=========================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}", "ERROR")
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=f"Morning briefing failed: {e}".encode("utf-8"),
                headers={"Title": "Briefing Failed", "Tags": "warning"},
                timeout=10,
            )
        except Exception:
            pass
        sys.exit(1)
