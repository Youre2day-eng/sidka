"""Reach skill — give the agent the internet, zero-config.

Inspired by Agent Reach (github.com/Panniantong/agent-reach): a capability layer
that lets the agent read the web, search, pull YouTube transcripts and RSS feeds,
and query GitHub — using free, keyless backends with graceful fallback. No API
keys or cookies for the core actions.
"""
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

NAME = "reach"
DESCRIPTION = (
    "Access the internet: read any web page as clean markdown, run a web search, "
    "pull a YouTube transcript, parse an RSS feed, or query GitHub. Keyless/zero-config "
    "(via Jina Reader/Search, yt-dlp, gh). Use whenever you need live information from "
    "the web, a URL's contents, a video's transcript, news/feeds, or repo details."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["read", "search", "youtube", "rss", "github", "doctor"],
            "description": "read=fetch a URL as markdown; search=web search; youtube=transcript; "
                           "rss=parse a feed; github=query a repo/search; doctor=backend status",
        },
        "url":   {"type": "string", "description": "URL for read / youtube / rss"},
        "query": {"type": "string", "description": "Search query, or GitHub repo (owner/name) / search terms"},
        "limit": {"type": "integer", "description": "Max results/entries (default 10) or max chars for read"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Sidka-Reach"
_MAX = 12000  # default cap for fetched text


def _get(url, timeout=25, accept=None):
    headers = {"User-Agent": _UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore"), None
    except Exception as e:
        return None, str(e)


def _post_form(url, fields, timeout=20):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore"), None
    except Exception as e:
        return None, str(e)


def _norm_url(u):
    u = (u or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


# ----------------------------------------------------------------- dispatch
def run(args):
    action = (args.get("action") or "read").lower()
    return {
        "read": _read, "search": _search, "youtube": _youtube,
        "rss": _rss, "github": _github, "doctor": _doctor,
    }.get(action, lambda a: f"Unknown action: {action}")(args)


def _read(args):
    url = _norm_url(args.get("url") or args.get("query"))
    if not url:
        return "Error: a url is required for read."
    cap = int(args.get("limit") or _MAX)
    # Jina Reader returns clean, LLM-friendly markdown of any page, no key needed.
    text, err = _get("https://r.jina.ai/" + url, timeout=30)
    if text is None:
        # fallback: fetch the raw page and strip tags crudely
        raw, err2 = _get(url, timeout=20)
        if raw is None:
            return f"Could not read {url}: {err or err2}"
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", raw, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    text = text.strip()
    note = "" if len(text) <= cap else f"\n\n…[truncated at {cap} chars]"
    return f"# Read: {url}\n\n{text[:cap]}{note}"


def _search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "Error: a query is required for search."
    n = int(args.get("limit") or 10)
    # Jina Search (s.jina.ai) returns ranked results as markdown, keyless.
    text, err = _get("https://s.jina.ai/" + urllib.parse.quote(q), timeout=30)
    if text and isinstance(text, str) and "rate limit" not in text.lower() and len(text.strip()) > 80:
        return f"# Search: {q}\n\n{text.strip()[:_MAX]}"
    # fallback: DuckDuckGo HTML (needs POST; redirect links carry the real URL in ?uddg=)
    html, err2 = _post_form("https://html.duckduckgo.com/html/", {"q": q}, timeout=20)
    if html is None:
        return f"Search failed for '{q}': {err or err2}"
    hits = re.findall(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    lines = [f"Search: {q}  (DuckDuckGo)"]
    for href, title in hits[:n]:
        m = re.search(r"uddg=([^&\"]+)", href)
        real = urllib.parse.unquote(m.group(1)) if m else href
        title = re.sub(r"<[^>]+>", "", title).strip()
        lines.append(f"- {title}\n  {real}")
    if len(lines) == 1:
        return f"No results parsed for '{q}'."
    return "\n".join(lines)


def _youtube(args):
    url = _norm_url(args.get("url") or args.get("query"))
    if not url:
        return "Error: a YouTube url is required."
    if shutil.which("yt-dlp"):
        try:
            import tempfile, glob
            d = tempfile.mkdtemp(prefix="reach_yt_")
            subprocess.run(
                ["yt-dlp", "--skip-download", "--write-auto-sub", "--write-sub",
                 "--sub-lang", "en.*", "--sub-format", "vtt", "-o", os.path.join(d, "s"), url],
                capture_output=True, timeout=90)
            vtts = glob.glob(os.path.join(d, "*.vtt"))
            if vtts:
                raw = open(vtts[0], encoding="utf-8", errors="ignore").read()
                # strip vtt timestamps/cue headers -> plain transcript, dedupe lines
                lines, seen = [], set()
                for ln in raw.splitlines():
                    ln = ln.strip()
                    if not ln or ln == "WEBVTT" or "-->" in ln or ln.isdigit():
                        continue
                    ln = re.sub(r"<[^>]+>", "", ln)
                    if ln and ln not in seen:
                        seen.add(ln); lines.append(ln)
                if lines:
                    body = " ".join(lines)
                    return f"# Transcript: {url}\n\n{body[:_MAX]}"
        except Exception as e:
            pass  # fall through
    # fallback: Jina Reader gets the title/description/metadata at least
    text, err = _get("https://r.jina.ai/" + url, timeout=30)
    if text:
        return (f"# YouTube (no transcript backend — install yt-dlp for captions): {url}\n\n"
                f"{text.strip()[:_MAX]}")
    return f"Could not access {url}: {err}. Install yt-dlp for transcripts: pip install yt-dlp"


def _rss(args):
    url = _norm_url(args.get("url") or args.get("query"))
    if not url:
        return "Error: a feed url is required for rss."
    n = int(args.get("limit") or 10)
    xml, err = _get(url, timeout=20, accept="application/rss+xml, application/xml, text/xml")
    if xml is None:
        return f"Could not fetch feed {url}: {err}"
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml.encode("utf-8", "ignore"))
    except Exception as e:
        return f"Could not parse feed {url}: {e}"
    items = []
    # RSS <item> and Atom <entry>
    for it in root.iter():
        tag = it.tag.split("}")[-1].lower()
        if tag in ("item", "entry"):
            def child(names):
                for c in it:
                    ct = c.tag.split("}")[-1].lower()
                    if ct in names:
                        if c.text and c.text.strip():
                            return c.text.strip()
                        if c.attrib.get("href"):
                            return c.attrib["href"]
                return ""
            items.append({
                "title": child(("title",)),
                "link": child(("link",)),
                "date": child(("pubdate", "published", "updated", "date")),
            })
    if not items:
        return f"No entries found in {url}."
    lines = [f"Feed: {url}  ({len(items)} entries)"]
    for e in items[:n]:
        when = f"  ({e['date']})" if e["date"] else ""
        lines.append(f"- {e['title']}{when}\n  {e['link']}")
    return "\n".join(lines)


def _github(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "Error: a repo (owner/name) or search terms required for github."
    if not shutil.which("gh"):
        return "gh CLI not installed. Install: brew install gh"
    try:
        if re.match(r"^[\w.-]+/[\w.-]+$", q):  # owner/name -> repo summary
            out = subprocess.run(
                ["gh", "repo", "view", q, "--json",
                 "name,description,stargazerCount,primaryLanguage,url,updatedAt,homepageUrl"],
                capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return f"gh error: {out.stderr.strip()[:300]}"
            import json
            r = json.loads(out.stdout)
            lang = (r.get("primaryLanguage") or {}).get("name", "—")
            return (f"# {r['name']}  ⭐{r.get('stargazerCount',0)}  [{lang}]\n"
                    f"{r.get('description','')}\n{r.get('url','')}\n"
                    f"updated {r.get('updatedAt','')[:10]}")
        # otherwise search repos
        out = subprocess.run(
            ["gh", "search", "repos", q, "--limit", str(int(args.get("limit") or 8)),
             "--json", "fullName,description,stargazersCount,url"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return f"gh error: {out.stderr.strip()[:300]}"
        import json
        rows = json.loads(out.stdout)
        lines = [f"GitHub search: {q}"]
        for r in rows:
            lines.append(f"- {r['fullName']}  ⭐{r.get('stargazersCount',0)}\n  "
                         f"{(r.get('description') or '').strip()[:100]}\n  {r['url']}")
        return "\n".join(lines)
    except Exception as e:
        return f"github query failed: {e}"


def _doctor(args):
    lines = ["=== Reach backend status ==="]
    # web read/search via Jina
    _, err = _get("https://r.jina.ai/https://example.com", timeout=10)
    lines.append(f"  web read (Jina Reader):   {'OK' if not err else 'unreachable — ' + err}")
    _, serr = _get("https://s.jina.ai/test", timeout=10)
    lines.append(f"  web search (Jina/DDG):    {'OK' if not serr else 'Jina down, DuckDuckGo fallback'}")
    lines.append(f"  youtube transcripts:      {'OK (yt-dlp)' if shutil.which('yt-dlp') else 'install yt-dlp (pip install yt-dlp)'}")
    lines.append(f"  rss feeds:                OK (stdlib)")
    lines.append(f"  github:                   {'OK (gh)' if shutil.which('gh') else 'install gh (brew install gh)'}")
    lines.append("\nCookie-based platforms (Twitter/Reddit/Xiaohongshu) are not enabled in this "
                 "zero-config build.")
    return "\n".join(lines)
