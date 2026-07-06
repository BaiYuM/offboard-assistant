from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"


SYSTEM_PROMPT = """You are a privacy-preserving offboarding cleanup reviewer.

You receive metadata only. It must not contain plaintext passwords, cookies, chat contents, or full API key values.

Your job:
- Recommend which items should be selected for cleanup review.
- Prefer temporary/cache items and obvious post-baseline work residue.
- For API key files, recommend revoke/rotate first, then local cleanup.
- Do not recommend automatic deletion for browser passwords, chat directories, SSH keys, cloud credentials, or ambiguous personal data.
- Return strict JSON only.
"""


def build_user_prompt(payload: dict[str, Any]) -> str:
    return (
        "Review this offboarding cleanup metadata and return JSON with this schema:\n"
        "{\n"
        '  "summary": "short Chinese summary",\n'
        '  "selected_ids": ["item id strings recommended for selection"],\n'
        '  "decisions": [\n'
        '    {"id": "item id", "action": "select|review|keep", "risk": "low|medium|high", "reason": "Chinese reason"}\n'
        "  ],\n"
        '  "warnings": ["Chinese warnings"]\n'
        "}\n\n"
        "Rules:\n"
        "- Select only items that are safe or useful to put into a human-reviewed cleanup queue.\n"
        "- For recommend_cleanup cache/temp items, action can be select with low/medium risk.\n"
        "- For secrets, action can be select, but reason must say revoke/rotate first.\n"
        "- For browser_login_metadata and chat_data_location, usually action should be review, not automatic cleanup.\n"
        "- Never invent IDs.\n\n"
        "Payload:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def normalize_review_result(data: dict[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    selected_ids = [str(item_id) for item_id in data.get("selected_ids", []) if str(item_id) in allowed_ids]
    decisions = []
    for decision in data.get("decisions", []):
        item_id = str(decision.get("id", ""))
        if item_id not in allowed_ids:
            continue
        decisions.append(
            {
                "id": item_id,
                "action": str(decision.get("action", "review")),
                "risk": str(decision.get("risk", "medium")),
                "reason": str(decision.get("reason", "")),
            }
        )
    return {
        "summary": str(data.get("summary", "")),
        "selected_ids": selected_ids,
        "decisions": decisions,
        "warnings": [str(warning) for warning in data.get("warnings", [])],
    }


def review_with_openai_compatible(
    *,
    api_key: str,
    base_url: str,
    model: str,
    payload: dict[str, Any],
    timeout: int = 60,
) -> dict[str, Any]:
    if not api_key.strip():
        raise ValueError("API key is required.")
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model or DEFAULT_MODEL
    allowed_ids = {str(item.get("id")) for item in payload.get("items", []) if item.get("id")}
    request_body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(payload)},
        ],
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key.strip()}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AI review request failed: {exc}") from exc
    content = response_body["choices"][0]["message"]["content"]
    return normalize_review_result(extract_json_object(content), allowed_ids)
