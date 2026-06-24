DESCRIPTION = "Fetch the content of a URL and return readable text (HTML tags stripped)"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch"
                    },
                    "raw": {
                        "type": "boolean",
                        "description": "If true return raw HTML, if false strip tags",
                        "default": False
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return",
                        "default": 6000
                    }
                },
                "required": ["url"]
            }
        }
    }
]


import urllib.request
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self._parts.append(data)

    def get_text(self):
        return " ".join(" ".join(self._parts).split())


def run(args: dict) -> str:
    url = args["url"]
    raw = args.get("raw", False)
    max_chars = args.get("max_chars", 6000)

    req = urllib.request.Request(url, headers={"User-Agent": "RunAI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error fetching {url}: {e}"

    if raw:
        text = html
    else:
        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()

    if len(text) > max_chars:
        return text[:max_chars] + "... (truncated)"
    return text
