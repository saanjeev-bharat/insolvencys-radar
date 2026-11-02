"""Turn raw notice / listing text into a structured lead.

Two layers:
  1. Deterministic regex extraction (fast, free, always runs).
  2. Optional Claude refinement (set ANTHROPIC_API_KEY) for messy text.
"""
import json
import os
import re

STATUS_PATTERNS = [
    ("liquidation", r"liquidator|winding[- ]up|wound up|liquidation"),
    ("administration", r"voluntary administrat|administrator appointed|deed of company arrangement"),
    ("receivership", r"receiver(?:s)?(?: and manager)?|controller appointed"),
    ("deregistration", r"deregistr|strike[- ]?off"),
    ("mortgagee_sale", r"mortgagee|mortgagee in possession"),
]

STATES = ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]

ABN_RE = re.compile(r"\bABN[:\s]*((?:\d[ ]?){11})\b", re.I)
ACN_RE = re.compile(r"\bACN[:\s]*((?:\d[ ]?){9})\b", re.I)
PHONE_RE = re.compile(r"(?:\(0\d\)\s?\d{4}\s?\d{4}|\b0[2-478]\s?\d{4}\s?\d{4}\b|\b04\d{2}\s?\d{3}\s?\d{3}\b|\b1[38]00\s?\d{3}\s?\d{3}\b)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b", re.I)
POSTCODE_RE = re.compile(r"\b(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+(\d{4})\b")
NAME_RE = re.compile(
    r"((?:[A-Z0-9][\w&'\-]*\s+){1,6}"
    r"(?:Pty\.?\s+Ltd\.?|PTY\.?\s+LTD\.?|Pty\.?\s+Limited|PTY\s+LIMITED|Limited|LIMITED|Ltd|LTD))"
)
PRACT_PATTERNS = [
    re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,2}) (?:was|were) (?:\w+ )?appointed"),
    re.compile(r"(?:Contact|Enquiries to|enquiries to)\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})"),
    re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,2}),?\s+(?:Joint\s+)?(?:Liquidator|Administrator|Receiver)s?\b"),
]
FIRM_RE = re.compile(r"\b((?:[A-Z][\w&]+\s){0,3}(?:Advisory|Restructuring|Recovery|Insolvency|Partners|Chartered Accountants|Business Recovery|Forensic))\b")

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _digits(s):
    return re.sub(r"\D", "", s) if s else None


def _iso_date(m):
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def classify(text: str) -> str | None:
    for status, pat in STATUS_PATTERNS:
        if re.search(pat, text, re.I):
            return status
    return None


def extract_regex(text: str, kind: str = "insolvency") -> dict:
    out = {"kind": kind, "raw_text": text[:4000]}
    out["status"] = classify(text)
    if m := ABN_RE.search(text):
        out["abn"] = _digits(m.group(1))
    if m := ACN_RE.search(text):
        out["acn"] = _digits(m.group(1))
    if m := NAME_RE.search(text):
        out["entity_name"] = " ".join(m.group(1).split()).strip(" ,.")
    if m := POSTCODE_RE.search(text):
        out["postcode"] = m.group(1)
        out["state"] = re.search(r"(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)", m.group(0)).group(1)
    else:
        for st in STATES:
            if re.search(rf"\b{st}\b", text):
                out["state"] = st
                break
    dates = [_iso_date(m) for m in DATE_RE.finditer(text)]
    if dates:
        out["notice_date"] = dates[0]
        if len(dates) > 1:
            out["deadline"] = dates[-1]
    if m := PHONE_RE.search(text):
        out["contact_phone"] = " ".join(m.group(0).split())
    if m := EMAIL_RE.search(text):
        out["contact_email"] = m.group(0).lower()
    for pat in PRACT_PATTERNS:
        if m := pat.search(text):
            out["contact_name"] = m.group(1).strip()
            break
    if m := FIRM_RE.search(text):
        out["contact_firm"] = m.group(1).strip()
    # confidence = share of key fields found
    keys = ["entity_name", "status", "state", "notice_date", "contact_phone", "contact_email", "contact_name"]
    out["confidence"] = round(sum(1 for k in keys if out.get(k)) / len(keys), 2)
    return out


LLM_PROMPT = """Extract structured data from this Australian insolvency notice or asset
listing. Return ONLY JSON with keys: entity_name, abn, acn, status
(liquidation|administration|receivership|deregistration|mortgagee_sale|auction),
state (NSW/VIC/QLD/WA/SA/TAS/ACT/NT), suburb, postcode, asset_summary,
notice_date (YYYY-MM-DD), deadline (YYYY-MM-DD), contact_name, contact_firm,
contact_phone, contact_email. Use null for anything not stated. Never guess
contact details; only copy what is in the text.

TEXT:
"""


def refine_with_claude(text: str, base: dict) -> dict:
    """Fill gaps using Claude. No-op without an API key."""
    if not os.environ.get("ANTHROPIC_API_KEY") or base.get("confidence", 0) >= 0.85:
        return base
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.environ.get("RADAR_MODEL", "claude-haiku-5-5"),
            max_tokens=800,
            messages=[{"role": "user", "content": LLM_PROMPT + text[:6000]}],
        )
        data = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))
    except Exception:
        return base
    merged = dict(base)
    for k, v in data.items():
        if v and not merged.get(k):
            merged[k] = v
    merged["confidence"] = min(1.0, base.get("confidence", 0) + 0.15)
    return merged
