"""
ARlab Services Refresh Agent.

Pulls every weekly newsletter from nourobeid7/arlab-newsletter, parses the
service catalogue from ../index.html, asks Claude Opus 4.7 to rewrite each
service's `desc` using the latest signals, then injects the new descs and the
current week's date label back into ../index.html.

Runs weekly via .github/workflows/intelligence.yml, one hour after the
newsletter. Can also run locally with ANTHROPIC_API_KEY in env.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
INDEX_HTML = REPO_ROOT / "index.html"
PROMPT_MD = HERE / "prompt.md"

NEWSLETTER_REPO = "nourobeid7/arlab-newsletter"
MODEL = "claude-opus-4-7"

GITHUB_API = "https://api.github.com"

SERVICE_KEY_ORDER = ("cat", "name", "status", "ai", "viability", "trend", "addon", "blurb", "desc")


def gh_headers() -> dict[str, str]:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "arlab-intelligence-agent"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def fetch_newsletters() -> list[dict]:
    """Pull every history/*.json from the newsletter repo, oldest to newest."""
    url = f"{GITHUB_API}/repos/{NEWSLETTER_REPO}/contents/history"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    r.raise_for_status()
    entries = sorted(
        (e for e in r.json() if e["name"].endswith(".json")),
        key=lambda e: e["name"],
    )
    out = []
    for e in entries:
        raw = requests.get(e["download_url"], headers=gh_headers(), timeout=30)
        raw.raise_for_status()
        payload = raw.json()
        payload["_date"] = e["name"].replace(".json", "")
        out.append(payload)
    print(f"[fetch] {len(out)} newsletters from {out[0]['_date']} → {out[-1]['_date']}", file=sys.stderr)
    return out


def find_services_block(index_html: str) -> tuple[int, int]:
    """Return (bracket_start, bracket_end_inclusive) for the SERVICES array `[...]`."""
    m = re.search(r"var SERVICES\s*=\s*\[", index_html)
    if not m:
        raise RuntimeError("SERVICES array not found in index.html")
    start = m.end() - 1  # position of '['
    depth = 0
    in_str = False
    esc = False
    quote = ""
    i = start
    while i < len(index_html):
        c = index_html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in ("'", '"'):
                in_str = True
                quote = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return start, i
        i += 1
    raise RuntimeError("SERVICES array not terminated")


def parse_services(index_html: str) -> list[dict]:
    """Extract SERVICES as a list of dicts."""
    start, end = find_services_block(index_html)
    arr_src = index_html[start : end + 1]
    normalized = arr_src
    for key in SERVICE_KEY_ORDER:
        normalized = re.sub(rf"([{{,]\s*){key}\s*:", rf'\1"{key}":', normalized)
    normalized = re.sub(r",(\s*[}\]])", r"\1", normalized)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as err:
        snippet = normalized[max(0, err.pos - 60) : err.pos + 60]
        raise RuntimeError(f"SERVICES JSON parse failed at {err.pos}: …{snippet}…") from err


def serialize_service(s: dict) -> str:
    """Render a service dict in the same JS-literal format as the source."""
    parts = []
    for k in SERVICE_KEY_ORDER:
        if k not in s:
            continue
        v = s[k]
        if isinstance(v, bool):
            parts.append(f"{k}:{'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}:{v}")
        else:
            parts.append(f"{k}:{json.dumps(v, ensure_ascii=False)}")
    return "{" + ",".join(parts) + "}"


def serialize_services_array(services: list[dict]) -> str:
    return "[\n" + ",\n".join("  " + serialize_service(s) for s in services) + ",\n]"


def inject_services(index_html: str, services: list[dict]) -> str:
    start, end = find_services_block(index_html)
    return index_html[:start] + serialize_services_array(services) + index_html[end + 1 :]


def inject_week_label(index_html: str, label: str) -> str:
    pattern = r'(<span class="date-label">)[^<]*(</span>)'
    new_html, n = re.subn(pattern, rf"\1{label}\2", index_html, count=1)
    if n == 0:
        raise RuntimeError("date-label span not found in index.html")
    return new_html


def current_week_label(today: datetime | None = None) -> str:
    today = (today or datetime.now(timezone.utc)).date()
    monday = today - timedelta(days=today.weekday())
    return f"Week of {monday.strftime('%B')} {monday.day}, {monday.year}"


def compact_newsletter(nl: dict) -> dict:
    """Trim a newsletter payload to just the signal-bearing fields."""

    def pick(items: list[dict] | None, keys: tuple[str, ...]) -> list[dict]:
        if not items:
            return []
        out = []
        for it in items:
            trimmed = {k: it[k] for k in keys if k in it and it[k]}
            if trimmed:
                out.append(trimmed)
        return out

    return {
        "date": nl["_date"],
        "signals": pick(nl.get("signals"), ("headline", "body", "arlab_use")),
        "coming_next": pick(nl.get("coming_next"), ("category", "headline", "body")),
        "patterns": pick(nl.get("patterns"), ("tag", "headline", "body")),
        "first_of_kind": pick(nl.get("first_of_kind"), ("name", "tag", "body")),
        "tools": pick(nl.get("tools"), ("name", "body")),
        "partners": pick(nl.get("partners"), ("category", "name", "body")),
        "action_plan": pick(nl.get("action_plan"), ("title", "body")),
    }


def build_tool_schema(service_names: list[str]) -> dict:
    return {
        "name": "submit_service_updates",
        "description": (
            "Submit refreshed descriptions for every ARlab service, grounded in the latest "
            "newsletter archive. One `updates` array; one entry per service."
        ),
        "input_schema": {
            "type": "object",
            "required": ["updates"],
            "properties": {
                "updates": {
                    "type": "array",
                    "minItems": len(service_names),
                    "maxItems": len(service_names),
                    "description": (
                        f"Exactly {len(service_names)} entries — one per service in the index, "
                        "in any order. Each entry rewrites the `desc` for one service."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["name", "desc"],
                        "properties": {
                            "name": {
                                "type": "string",
                                "enum": service_names,
                                "description": "Exact service name from the ARlab index.",
                            },
                            "desc": {
                                "type": "string",
                                "description": (
                                    "~300–400 character refreshed description. Plain text, "
                                    "no markdown, no newlines. Dense with current proof points "
                                    "(brands, products, prices, dates, metrics)."
                                ),
                            },
                        },
                    },
                },
            },
        },
    }


def call_agent(system_prompt: str, newsletters: list[dict], services: list[dict]) -> list[dict]:
    client = Anthropic()
    service_names = [s["name"] for s in services]
    tool = build_tool_schema(service_names)
    archive = [compact_newsletter(nl) for nl in newsletters]
    user_block = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "## The archive\n\n"
                    f"{len(archive)} weekly newsletters, {archive[0]['date']} → {archive[-1]['date']}.\n\n"
                    "```json\n" + json.dumps(archive, ensure_ascii=False) + "\n```\n\n"
                    "## The ARlab services index\n\n"
                    f"{len(services)} services currently offered. Each has viability, trend, and the "
                    "existing desc you are refreshing.\n\n"
                    "```json\n" + json.dumps(services, ensure_ascii=False) + "\n```\n\n"
                    "## Your task\n\n"
                    "Rewrite the `desc` of every service to reflect the freshest newsletter "
                    "signals. Match the existing voice and length. Call the "
                    "`submit_service_updates` tool once with one entry per service — nothing else."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    resp = client.messages.create(
        model=MODEL,
        max_tokens=24000,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_service_updates"},
        messages=[user_block],
    )
    print(f"[agent] usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}", file=sys.stderr)
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_service_updates":
            return block.input["updates"]
    raise RuntimeError("agent did not call submit_service_updates")


def apply_updates(services: list[dict], updates: list[dict]) -> list[dict]:
    """Merge new descs into the services list by name. Fail hard on mismatches."""
    by_name = {u["name"]: u["desc"] for u in updates}
    service_names = {s["name"] for s in services}
    missing = service_names - by_name.keys()
    extra = by_name.keys() - service_names
    if missing:
        raise RuntimeError(f"agent did not update {len(missing)} services: {sorted(missing)}")
    if extra:
        raise RuntimeError(f"agent returned updates for unknown services: {sorted(extra)}")
    out = []
    for s in services:
        new_s = dict(s)
        new_s["desc"] = by_name[s["name"]]
        out.append(new_s)
    return out


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2
    newsletters = fetch_newsletters()
    html = INDEX_HTML.read_text()
    services = parse_services(html)
    system_prompt = PROMPT_MD.read_text()
    print(f"[input] {len(newsletters)} newsletters · {len(services)} services", file=sys.stderr)
    t0 = time.time()
    updates = call_agent(system_prompt, newsletters, services)
    elapsed = time.time() - t0
    print(f"[agent] {len(updates)} desc updates in {elapsed:.1f}s", file=sys.stderr)
    refreshed = apply_updates(services, updates)
    new_html = inject_services(html, refreshed)
    label = current_week_label()
    new_html = inject_week_label(new_html, label)
    INDEX_HTML.write_text(new_html)
    print(f"[write] {INDEX_HTML} refreshed · label: {label}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
