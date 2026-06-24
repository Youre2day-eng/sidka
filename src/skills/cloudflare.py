"""Cloudflare Pages skill: list deployments or trigger a new deploy."""
from _lib import http, secret

NAME = "cloudflare"
DESCRIPTION = (
    "Cloudflare Pages. 'deployments' lists the latest deploys for a project; "
    "'deploy' triggers a new production deployment. Needs cf_api_token and cf_account_id "
    "in secrets. project is the Pages project name, e.g. fsbacktrack."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action":  {"type": "string", "enum": ["deployments", "deploy"]},
        "project": {"type": "string", "description": "Pages project name, e.g. fsbacktrack"},
    },
    "required": ["action", "project"],
}


def run(args):
    tok = secret("cf_api_token")
    acct = secret("cf_account_id")
    if not (tok and acct):
        return "Add \"cf_api_token\" and \"cf_account_id\" to ~/.runai/secrets.json."
    project = args.get("project")
    if not project:
        return "Need a 'project' name."
    H = {"Authorization": f"Bearer {tok}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/pages/projects/{project}/deployments"
    action = (args.get("action") or "").lower()

    if action == "deployments":
        r = http("GET", base, headers=H)
        res = r.get("result") if isinstance(r, dict) else None
        if not res:
            return f"Cloudflare error: {r}"
        rows = []
        for d in res[:5]:
            stage = (d.get("latest_stage") or {}).get("status", "?")
            rows.append(f"{d.get('short_id', '?')}  {d.get('environment', '?')}  {stage}  {str(d.get('created_on', ''))[:19]}")
        return "\n".join(rows) or "(no deployments)"

    if action == "deploy":
        r = http("POST", base, headers=H)
        if isinstance(r, dict) and r.get("success"):
            d = r.get("result", {})
            stage = (d.get("latest_stage") or {}).get("status", "building")
            return f"Deploy queued: {d.get('short_id', '?')} ({stage})."
        return f"Cloudflare error: {r}"

    return f"Unknown action: {action}"
