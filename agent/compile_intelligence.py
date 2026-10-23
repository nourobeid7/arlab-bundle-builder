"""
ARlab Market Intelligence Agent.

Pulls every weekly newsletter from nourobeid7/arlab-newsletter, parses the
40-service index from ../index.html, hands both to Claude Opus 4.7 via tool-use,
and writes the structured forecast to ../intelligence.json.

Runs weekly via .github/workflows/intelligence.yml, one hour after the newsletter.
Can also run locally with ANTHROPIC_API_KEY in env.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
INDEX_HTML = REPO_ROOT / "index.html"
OUT_JSON = REPO_ROOT / "intelligence.json"
PROMPT_MD = HERE / "prompt.md"

NEWSLETTER_REPO = "nourobeid7/arlab-newsletter"
MODEL = "claude-opus-4-7"

GITHUB_API = "https://api.github.com"


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


def parse_services(index_html: str) -> list[dict]:
    """Extract the inline `var SERVICES = [ ... ]` block from index.html."""
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
                    break
        i += 1
    arr_src = index_html[start : i + 1]
    # Convert JS object literals (unquoted keys, single quotes) to JSON.
    # Keys in SERVICES are the fixed set: cat, name, status, ai, viability, trend, desc.
    normalized = arr_src
    # Quote keys
    for key in ("cat", "name", "status", "ai", "viability", "trend", "desc", "addon"):
        normalized = re.sub(rf"([{{,]\s*){key}\s*:", rf'\1"{key}":', normalized)
    # Strip JS-legal trailing commas before } or ] — JSON forbids them.
    normalized = re.sub(r",(\s*[}\]])", r"\1", normalized)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as err:
        snippet = normalized[max(0, err.pos - 60) : err.pos + 60]
        raise RuntimeError(f"SERVICES JSON parse failed at {err.pos}: …{snippet}…") from err


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


DOMAINS = [
    "Creator Economy",
    "AI Creative",
    "Smart Glasses & On-Face AR",
    "Live Commerce & Immersive Retail",
    "GCC Sovereign AI",
    "Agentic Workflows",
]


def build_tool_schema() -> dict:
    return {
        "name": "submit_intelligence",
        "description": (
            "Submit ARlab's compiled market intelligence for this week. "
            "One headline + six domain cards + five dates. That's the whole dashboard."
        ),
        "input_schema": {
            "type": "object",
            "required": ["headline", "whats_next", "dates_to_watch"],
            "properties": {
                "headline": {
                    "type": "object",
                    "required": ["h", "b"],
                    "description": "The editorial lede for the week.",
                    "properties": {
                        "h": {
                            "type": "string",
                            "description": "8–14 words. The one thing the CEO needs to know this week.",
                        },
                        "b": {
                            "type": "string",
                            "description": "1–2 sentences expanding the headline. Must name 2–3 specific proof points.",
                        },
                    },
                },
                "whats_next": {
                    "type": "array",
                    "minItems": 6,
                    "maxItems": 6,
                    "description": "Exactly six domain cards, in the fixed domain order. Visual-first: curve shape + heat + one-line verdict.",
                    "items": {
                        "type": "object",
                        "required": [
                            "domain",
                            "shape",
                            "heat",
                            "verdict",
                            "arlab_play",
                            "services",
                            "price_anchor",
                        ],
                        "properties": {
                            "domain": {"type": "string", "enum": DOMAINS},
                            "shape": {
                                "type": "string",
                                "enum": ["exp_up", "s_rise", "flat_rise", "tri_rise", "consolidate", "exp_down", "plateau"],
                                "description": "The trajectory shape — the diagram carries the meaning.",
                            },
                            "heat": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                                "description": "0–100 raw intensity this quarter. 90+ = on fire this week; 50 = baseline; <30 = cooling.",
                            },
                            "verdict": {
                                "type": "string",
                                "description": "10–18 word punch line. One takeaway. Must name a brand / product / date OR an ARlab service.",
                            },
                            "arlab_play": {
                                "type": "string",
                                "description": "10–18 word directive — pitch / ship / reprice / kill. Must name an exact ARlab service.",
                            },
                            "services": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {"type": "string"},
                                "description": "Exact service names from the ARlab index.",
                            },
                            "price_anchor": {
                                "type": "string",
                                "description": "Short pricing label e.g. '$8–15K / mo', 'internal R&D'.",
                            },
                        },
                    },
                },
                "dates_to_watch": {
                    "type": "array",
                    "minItems": 5,
                    "maxItems": 5,
                    "description": "Exactly 5 upcoming dates/events that will close or kill deals.",
                    "items": {
                        "type": "object",
                        "required": ["when", "what", "why_it_matters"],
                        "properties": {
                            "when": {"type": "string"},
                            "what": {"type": "string"},
                            "why_it_matters": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def call_agent(system_prompt: str, newsletters: list[dict], services: list[dict]) -> dict:
    client = Anthropic()
    tool = build_tool_schema()
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
                    f"{len(services)} services currently offered. Each has viability, trend, and recent context.\n\n"
                    "```json\n" + json.dumps(services, ensure_ascii=False) + "\n```\n\n"
                    "## Your task\n\n"
                    "Compile this week's ARlab market intelligence. Call the `submit_intelligence` "
                    "tool once with the full structured payload. Everything you emit lives inside "
                    "that tool call — no free text."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_intelligence"},
        messages=[user_block],
    )
    print(f"[agent] usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}", file=sys.stderr)
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_intelligence":
            return block.input
    raise RuntimeError("agent did not call submit_intelligence")


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2
    newsletters = fetch_newsletters()
    services = parse_services(INDEX_HTML.read_text())
    system_prompt = PROMPT_MD.read_text()
    print(f"[input] {len(newsletters)} newsletters · {len(services)} services", file=sys.stderr)
    t0 = time.time()
    intelligence = call_agent(system_prompt, newsletters, services)
    elapsed = time.time() - t0
    print(f"[agent] completed in {elapsed:.1f}s", file=sys.stderr)
    envelope = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "based_on": {
            "newsletters": len(newsletters),
            "range_start": newsletters[0]["_date"],
            "range_end": newsletters[-1]["_date"],
            "services": len(services),
            "model": MODEL,
        },
        **intelligence,
    }
    OUT_JSON.write_text(json.dumps(envelope, ensure_ascii=False, indent=2))
    print(f"[write] {OUT_JSON} ({OUT_JSON.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
