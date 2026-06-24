"""Webhook skill: POST to any webhook URL (Slack/Discord, or FSWorkpad automations)."""
from _lib import http

NAME = "webhook"
DESCRIPTION = (
    "POST a payload to a webhook URL. Use for Slack or Discord incoming webhooks, or the "
    "user's FSWorkpad automation webhooks (https://fsworkpad.pages.dev/api/webhooks/...). "
    "Give 'text' for a simple message, or 'payload' for a full JSON object."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "url":     {"type": "string"},
        "text":    {"type": "string", "description": "simple message, sent as {\"text\": ...}"},
        "payload": {"type": "object", "description": "full JSON body to send instead of text"},
    },
    "required": ["url"],
}


def run(args):
    url = args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    body = args.get("payload")
    if body is None:
        body = {"text": args["text"]} if args.get("text") else {}
    resp = http("POST", url, body=body)
    if isinstance(resp, dict) and resp.get("_error"):
        return f"Webhook failed ({resp.get('_status', '?')}): {resp['_error']}"
    return f"POSTed to {url}. Response: {str(resp)[:600]}"
