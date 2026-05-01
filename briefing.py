#!/usr/bin/env python3
"""
Morning Briefing - GitHub Actions Edition
Fetches RSS news, generates AI briefing via Claude, publishes to Telegraph, sends ntfy push.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import feedparser
import requests

# -- Config ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "con-hillier-morning-briefing")
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

RSS_FEEDS = [
    {"url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "World"},
    {"url": "http://feeds.bbci.co.uk/news/business/rss.xml", "category": "Business"},
    {"url": "http://feeds.bbci.co.uk/news/technology/rss.xml", "category": "Technology"},
    {"url": "http://feeds.bbci.co.uk/news/scotland/rss.xml", "category": "Scotland"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "AI"},
    {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "Markets"},
]

# -- Logging -----------------------------------------------------------------
def log(message, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {message}")


# -- Text Sanitizer ----------------------------------------------------------
def sanitize_text(text):
    """Strip non-ASCII characters, replacing common ones with ASCII equivalents."""
    if not text:
        return text
    # Normalize unicode (e.g. decompose accented chars)
    text = unicodedata.normalize("NFKD", text)
    # Smart quotes -> straight
    for ch in "‘’‚‛":
        text = text.replace(ch, "'")
    for ch in "“”„‟":
        text = text.replace(ch, '"')
    # Dashes
    text = text.replace("–", "-")       # en dash
    text = text.replace("—", " - ")     # em dash
    text = text.replace("―", " - ")     # horizontal bar
    # Ellipsis
    text = text.replace("…", "...")
    # Bullets
    text = text.replace("•", "-")
    text = text.replace("‣", "-")
    # Non-breaking space
    text = text.replace(" ", " ")
    # Guillemets
    text = text.replace("«", '"')
    text = text.replace("»", '"')
    # Strip combining diacritical marks (left over from NFKD normalization)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    # Nuclear option: drop anything remaining outside printable ASCII
    text = "".join(ch if 32 <= ord(ch) <= 126 else "" for ch in text)
    return text


# -- RSS Feed Fetching -------------------------------------------------------
def fetch_feed(url, category, max_items=8):
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            # Strip HTML tags from description
            desc = re.sub(r"<[^>]+>", "", entry.get("summary", entry.get("description", "")))
            desc = desc.replace("&amp;", "&").replace("&nbsp;", " ").strip()
            link = entry.get("link", "").strip()
            items.append({"title": title, "description": desc, "link": link, "category": category})
        log(f"Fetched {len(items)} items from {category}")
        return items
    except Exception as e:
        log(f"Failed to fetch {category} feed: {e}", "WARN")
        return []


def fetch_all_news():
    all_news = []
    for feed in RSS_FEEDS:
        all_news.extend(fetch_feed(feed["url"], feed["category"]))
    log(f"Total news items fetched: {len(all_news)}")
    return all_news


# -- Claude AI Briefing ------------------------------------------------------
def generate_briefing(news_items, api_key):
    uk_tz = timezone(timedelta(hours=0))  # Use UTC, close enough for date display
    today = datetime.now(uk_tz).strftime("%A %d %B %Y")

    news_text = ""
    for item in news_items:
        news_text += f"[{item['category']}] {item['title']}\n"
        if item["description"]:
            news_text += f"{item['description']}\n"
        news_text += "\n"

    user_prompt = f"""Here are today's raw news headlines and summaries from RSS feeds:

{news_text}

Write a professional morning briefing following these rules exactly:

STORY SELECTION: Pick the 5 most consequential stories. Ensure a mix across geopolitics, business/markets, and technology/AI. Never combine unrelated events into one story.

STORY FORMAT: For each story, write exactly 2 paragraphs:
- Paragraph 1: What happened. Lead with the single most important fact. Be specific - name the person, the number, the country. When citing any figure ($, %, points), immediately contextualise it (compared to what? is that a lot? what does it mean for a normal person?).
- Paragraph 2: Why it matters. One clear "so what" - the implication, the risk, the opportunity. No padding, no vague gestures at "broader instability." End with a concrete forward-looking point.

HEADLINES: Short, punchy, no filler words. Maximum 8 words. Never use a colon. Good: "Oil Surges 8% on Iran Strike Fears". Bad: "Oil surges as Trump weighs Iran strikes - fertiliser crisis looms".

CLOSER TO HOME: Include genuinely significant Scotland, Glasgow, or UK stories that your reader should know about. Can be one story or several if warranted - but each must be significant enough to stand on its own. Write each as a short paragraph. Don't pad with filler - if nothing meaningful happened locally, set to null.

QUICK HITS: 4-5 other notable stories, each a single sentence of max 15 words. No editorialising - just the fact.

BOTTOM LINE: Exactly 2 sentences. Be opinionated and sharp. Synthesise the day's theme, don't just list what happened. Write like a smart friend texting you, not a news anchor.

QUALITY RULES:
- Never let one story's content bleed into another story
- Never include URLs or hyperlinks in the text
- Never reference the source (don't write "BBC reports" or "according to CNBC")
- Write for a smart adult who doesn't track markets daily
- Every sentence must earn its place - cut anything that doesn't add information

Return ONLY valid JSON:
{{
  "stories": [
    {{"headline": "Max 8 words no colon", "body": "Paragraph one.\\n\\nParagraph two."}}
  ],
  "closer_to_home": [{{"headline": "...", "body": "One tight paragraph"}}] or null,
  "quick_hits": ["Single sentence max 15 words each"],
  "bottom_line": "Two sharp opinionated sentences."
}}"""

    system_prompt = f"""You are an elite briefing writer producing a daily intelligence-style morning briefing. Your reader is a busy professional in Glasgow, Scotland. Today is {today}.

Your writing style: The Economist meets Morning Brew. Authoritative but conversational. Every sentence is tight, specific, and earns its place. You contextualise every number. You never pad, never waffle, never use cliches like "remains to be seen" or "only time will tell."

You return ONLY valid JSON. No markdown fences, no commentary, no extra text before or after the JSON.

ENCODING: Use ONLY ASCII characters (codes 32-126). Straight quotes, hyphens not dashes, ... not ellipsis. Spell accented names without accents. Use $, %, & freely."""

    request_body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                json=request_body,
                headers=headers,
                timeout=90,
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]

            # Strip markdown code fences if wrapped
            text = re.sub(r"^\s*```json\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)

            briefing = json.loads(text)
            if not briefing.get("stories"):
                raise ValueError("Claude returned no stories")

            log(f"Claude generated {len(briefing['stories'])} stories (attempt {attempt})")
            return briefing

        except Exception as e:
            log(f"Claude attempt {attempt} failed: {e}", "WARN")
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Claude failed after {MAX_RETRIES} attempts: {e}")
            delay = attempt * 10
            log(f"Retrying in {delay} seconds...")
            time.sleep(delay)


# -- Fallback Briefing -------------------------------------------------------
def fallback_briefing(news_items):
    log("Using fallback (no AI) briefing from RSS headlines")
    world = [i for i in news_items if i["category"] == "World"][:2]
    biz = [i for i in news_items if i["category"] in ("Business", "Markets")][:2]
    tech = [i for i in news_items if i["category"] in ("Technology", "AI")][:1]
    scotland = [i for i in news_items if i["category"] == "Scotland"][:1]

    stories = []
    for item in (world + biz + tech)[:5]:
        stories.append({"headline": item["title"], "body": item["description"] or item["title"]})

    closer_to_home = None
    if scotland:
        s = scotland[0]
        closer_to_home = {"headline": s["title"], "body": s["description"] or s["title"]}

    quick_hits = [i["title"] for i in news_items[5:10]]

    return {
        "stories": stories,
        "closer_to_home": closer_to_home,
        "quick_hits": quick_hits,
        "bottom_line": "Today's top stories at a glance. AI summary unavailable - check back tomorrow.",
    }


# -- Telegraph Publishing ----------------------------------------------------
def publish_to_telegraph(briefing):
    uk_tz = timezone(timedelta(hours=0))
    now = datetime.now(uk_tz)
    today = now.strftime("%A %d %B %Y")
    date_suffix = now.strftime("%d%b%H%M")

    # Build Telegraph node array
    nodes = []
    nodes.append({"tag": "p", "children": [{"tag": "em", "children": [f"{today} | 4-minute read"]}]})
    nodes.append({"tag": "hr"})

    for i, story in enumerate(briefing["stories"], 1):
        nodes.append({"tag": "h3", "children": [sanitize_text(f"{i}. {story['headline']}")]})
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", sanitize_text(story["body"])) if p.strip()]
        for p in paragraphs:
            nodes.append({"tag": "p", "children": [p]})
        nodes.append({"tag": "hr"})

    if briefing.get("closer_to_home"):
        cth = briefing["closer_to_home"]
        nodes.append({"tag": "h3", "children": [sanitize_text("Closer to Home")]})
        # Handle both array format and legacy single-object format
        cth_items = cth if isinstance(cth, list) else [cth]
        for item in cth_items:
            nodes.append({"tag": "p", "children": [{"tag": "strong", "children": [sanitize_text(item["headline"])]}]})
            paragraphs = [p.strip() for p in re.split(r"\n{2,}", sanitize_text(item["body"])) if p.strip()]
            for p in paragraphs:
                nodes.append({"tag": "p", "children": [p]})
        nodes.append({"tag": "hr"})

    if briefing.get("quick_hits"):
        nodes.append({"tag": "h4", "children": ["Quick Hits"]})
        for hit in briefing["quick_hits"]:
            nodes.append({"tag": "p", "children": [f"- {sanitize_text(hit)}"]})
        nodes.append({"tag": "hr"})

    nodes.append({"tag": "h3", "children": ["Bottom Line"]})
    nodes.append({"tag": "p", "children": [{"tag": "strong", "children": [sanitize_text(briefing["bottom_line"])]}]})
    nodes.append({"tag": "hr"})
    nodes.append({"tag": "p", "children": [{"tag": "em", "children": ["Your morning briefing, delivered daily."]}]})

    # Create Telegraph account
    acct_resp = requests.get(
        f"https://api.telegra.ph/createAccount?short_name=ConBrief{date_suffix}&author_name=Morning%20Briefing",
        timeout=REQUEST_TIMEOUT,
    )
    acct_data = acct_resp.json()
    if not acct_data.get("ok"):
        raise RuntimeError(f"Telegraph account creation failed: {acct_data}")
    token = acct_data["result"]["access_token"]
    log("Telegraph account created")

    # Create page - use JSON POST with proper UTF-8 (Python handles this natively)
    page_resp = requests.post(
        "https://api.telegra.ph/createPage",
        json={
            "access_token": token,
            "title": sanitize_text(f"Morning Briefing - {today}"),
            "author_name": "Con's Daily Briefing",
            "content": nodes,
            "return_content": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    page_data = page_resp.json()
    if not page_data.get("ok"):
        raise RuntimeError(f"Telegraph page creation failed: {page_data}")

    url = page_data["result"]["url"]
    log(f"Telegraph page published: {url}")
    return url


# -- ntfy Push Notification --------------------------------------------------
def send_ntfy(url, briefing):
    uk_tz = timezone(timedelta(hours=0))
    date_short = datetime.now(uk_tz).strftime("%a %d %b")

    body_lines = []
    for i, story in enumerate(briefing["stories"], 1):
        body_lines.append(f"**{i}.** {story['headline']}")
    if briefing.get("closer_to_home"):
        cth = briefing["closer_to_home"]
        cth_items = cth if isinstance(cth, list) else [cth]
        for item in cth_items:
            body_lines.append(f"**UK:** {item['headline']}")
    body_lines.append("")
    body_lines.append(f"[Read the full briefing here]({url})")
    ntfy_body = "\n".join(body_lines)

    resp = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=ntfy_body.encode("utf-8"),
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


# -- Main --------------------------------------------------------------------
def main():
    log("=========================================")
    log("=== Morning briefing started ===")

    if not ANTHROPIC_API_KEY:
        log("WARNING: ANTHROPIC_API_KEY not set. Will use fallback (no AI) mode.", "WARN")

    # Step 1: Fetch news
    log("Fetching RSS feeds...")
    all_news = fetch_all_news()
    if not all_news:
        log("No news items from any feed. Aborting.", "ERROR")
        sys.exit(1)

    # Step 2: Generate briefing
    briefing = None
    if ANTHROPIC_API_KEY:
        try:
            log("Generating AI briefing with Claude...")
            briefing = generate_briefing(all_news, ANTHROPIC_API_KEY)
        except Exception as e:
            log(f"Claude failed, falling back to RSS-only: {e}", "WARN")

    if not briefing:
        briefing = fallback_briefing(all_news)

    # Step 3: Publish to Telegraph
    log("Publishing to Telegraph...")
    url = publish_to_telegraph(briefing)

    # Step 4: Send push notification
    log("Sending push notification...")
    send_ntfy(url, briefing)

    log("=== Morning briefing complete ===")
    log("=========================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}", "ERROR")
        # Try to send error notification
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=f"Morning briefing failed: {e}",
                headers={"Title": "Briefing Failed", "Tags": "warning"},
                timeout=10,
            )
        except Exception:
            pass
        sys.exit(1)
