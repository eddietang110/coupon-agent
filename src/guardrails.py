"""Deterministic guardrails. No LLM is consulted here, on purpose:
a guardrail an attacker can talk to is not a guardrail.

Layer 1 (input)  : normalise untrusted review text, then screen for injection.
Layer 2 (output) : validate every field of the model's decision before it can
                   become a coupon, and fall back to no-send on any violation.
"""
import re
import unicodedata

import policy

# ---------------------------------------------------------------- input layer

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

# (family, pattern, case_sensitive). Case-insensitive unless flagged, matching
# the false-positive audit against the real review dataset (see coupon-agent
# notes): "Dan Dan noodles" is a menu item, not the DAN jailbreak persona, so
# that one needs literal-case matching rather than a blanket \bDAN\b, re.I.
INJECTION_PATTERNS = [
    ("instruction_override", r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your)\b[^.\n]{0,30}\b(instruction|prompt|rule|direction|guideline|polic)", False),
    ("instruction_override", r"\bnew\s+(instruction|rule|task|system\s+prompt)s?\b", False),
    ("role_manipulation",    r"\b(you\s+are\s+now|act\s+as(?!\s+if\b)|pretend\s+to\s+be|roleplay\s+as|from\s+now\s+on\s+you)\b", False),
    ("role_manipulation",    r"\b(developer|admin|root|god)\s+mode\b|\bjailbreak", False),
    ("role_manipulation",    r"\bDAN\b", True),  # case-sensitive: "Dan" is also just a name
    ("fake_authority",       r"\b(system|admin|administrator|manager|owner|developer|openai|anthropic)\s*[:>\]](?!\)|\()", False),
    ("fake_authority",       r"\[\s*(system|admin|instruction|important)\s*\]|<\s*(system|im_start|\|system\|)", False),
    ("fake_authority",       r"\bthis\s+is\s+(the|your)\s+(system|admin|developer|ceo|owner)\b", False),
    ("prompt_extraction",    r"\b(reveal|print|repeat|show|output|dump|leak)\b[^.\n]{0,30}\b(system\s+prompt|instructions|prompt|rules|api\s*key|token)\b", False),
    ("delimiter_escape",     r"(```|\"\"\"|---\s*end|<<<|>>>|\bEND\s+OF\s+REVIEW\b|</review>)", False),
    ("output_hijack",        r"\b(respond|reply|answer|return|output|set)\b[^.\n]{0,40}\b(json|only\s+with|exactly|verbatim|following\s+text)\b", False),
    # Reward coercion, tightened: a bare "give ... discount" shows up constantly
    # in ordinary reviews narrating a real staff discount ("he agreed to give
    # a refund"). Require a superlative/absolute qualifier, which is what every
    # genuine coercion attempt in the audit actually used.
    ("reward_coercion",      r"\b(give|send|issue|grant|award|approve)\b[^.\n]{0,30}\b(maximum|max|largest|highest|top|full[- ]?value|unlimited)\b[^.\n]{0,20}\b(coupon|voucher|refund|discount|offer|tier|value)\b", False),
    ("reward_coercion",      r"(\$\s?\d{3,}|\b(maximum|max|highest|unlimited|infinite)\b[^.\n]{0,20}\b(coupon|amount|discount|value)\b)", False),
    ("reward_coercion",      r"\bset\s+(the\s+)?(amount|amt|coupon)\b|\bsend\s*=\s*true\b|\"(amt|send|discount)\"\s*:\s*(true|false|\d)", False),
    ("encoded_payload",      r"\b(base64|rot13|atob|eval|<script|javascript:)\b", False),
    ("encoded_payload",      r"(?:[A-Za-z0-9+/]{40,}={0,2})", "entropy"),  # validated below, not a plain regex hit
    ("data_exfiltration",    r"https?://|\bwww\.[a-z0-9-]+\.|\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", False),
]
_COMPILED = [(k, re.compile(p, re.I if flag is False else 0), flag) for k, p, flag in INJECTION_PATTERNS]


def _has_entropy(s, min_unique=16, min_digits=2, max_top_char_ratio=0.12):
    """Distinguish a real encoded payload from keyboard-mash spam
    ("gggggg...", "johohohoho...") that happens to be 40+ chars of [A-Za-z0-9+/].
    Genuine base64 has near-uniform character distribution and typically a
    couple of digits; spam text is a tiny alphabet repeated in a short cycle,
    so its most frequent character covers a large share of the string.
    """
    from collections import Counter
    if len(set(s)) < min_unique or sum(c.isdigit() for c in s) < min_digits:
        return False
    top = Counter(s.lower()).most_common(1)[0][1]
    return top / len(s) <= max_top_char_ratio


def normalize(text):
    """Fold the tricks that let a payload hide from a literal pattern match."""
    if text is None:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).translate(_ZERO_WIDTH)
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"(.)\1{4,}", r"\1\1\1", t)          # sssspaced-out padding
    return t.strip()


def screen_input(text):
    """Return (is_injection, [pattern_names]) for one review."""
    t = normalize(text)
    hits = set()
    for name, rx, flag in _COMPILED:
        if flag == "entropy":
            if any(_has_entropy(m.group(0)) for m in rx.finditer(t)):
                hits.add(name)
        elif rx.search(t):
            hits.add(name)
    hits = sorted(hits)
    return bool(hits), hits


def wrap_untrusted(review_id, text, max_chars=600):
    """Fence untrusted content with a per-run nonce so escape attempts are inert."""
    t = normalize(text)
    if len(t) > max_chars:
        t = t[:max_chars] + "…"
    t = t.replace("\n", " ")
    return {"id": review_id, "text": t}


# --------------------------------------------------------------- output layer

URL_RX   = re.compile(r"https?://|www\.", re.I)
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)
PHONE_RX = re.compile(r"\+?\d[\d\s().-]{8,}\d")
MONEY_RX = re.compile(r"\$\s?(\d+(?:\.\d+)?)")
LEAK_RX  = re.compile(r"\b(system prompt|as an ai|language model|json|reason_code|assistant|instruction)\b", re.I)
PROMISE_RX = re.compile(r"\b(refund|free (meal|food|dinner) for life|lifetime|guarantee[ds]?|unlimited|lawsuit|legal action|compensat)\w*\b", re.I)


def _echoes_review(msg, review, n=8):
    """Catch the model parroting an injected sentence back into customer text."""
    rw, mw = normalize(review).lower().split(), normalize(msg).lower().split()
    if len(rw) < n or len(mw) < n:
        return False
    grams = {" ".join(rw[i:i + n]) for i in range(len(rw) - n + 1)}
    return any(" ".join(mw[i:i + n]) in grams for i in range(len(mw) - n + 1))


def validate_output(d, review, rating, followers, injected):
    """Check one decision. Returns (decision, violations, needs_repair).

    Repairable problems (amount out of band, inconsistent send flag) are fixed
    in place. A fatal problem is one in the customer-facing message: the caller
    gets one repair attempt, then the row falls back to sending nothing.
    """
    v, fatal = [], []          # v: repaired deterministically. fatal: message unusable.
    if not isinstance(d, dict):
        return ({"send": False, "amt": 0, "rc": "UNVERIFIABLE", "why": "", "msg": "", "conf": 0.0},
                ["missing or unparseable decision object"], True)

    rc = d.get("rc")
    if rc not in policy.REASON_CODES:
        v.append(f"unknown reason code {rc!r}")
        rc = "UNVERIFIABLE"

    # The input guardrail outranks anything the model decided.
    if injected:
        rc = "POLICY_BLOCK"
        if d.get("send"):
            v.append("model approved a coupon on an injected review; forced to no-send")

    amt = d.get("amt")
    if not isinstance(amt, int) or amt not in policy.TIERS:
        v.append(f"amount {amt!r} outside tier set")
        amt = 0
    amt, note = policy.clamp(amt, rc, rating, followers)
    if note:
        v.append(note)

    send = bool(d.get("send")) and amt > 0 and rc != "POLICY_BLOCK"
    if bool(d.get("send")) != send:
        v.append("send flag inconsistent with amount or policy; corrected")
    if not send:
        amt = 0

    why = str(d.get("why") or "").strip()
    msg = str(d.get("msg") or "").strip()

    if send:
        if not why:
            fatal.append("no reason supplied for a sent coupon")
        elif why.lower() not in msg.lower() and not any(
                w in msg.lower() for w in re.findall(r"[a-z]{4,}", why.lower())):
            fatal.append("customer message does not state the reason")
        if not (policy.MSG_MIN_CHARS <= len(msg) <= policy.MSG_MAX_CHARS):
            fatal.append(f"message length {len(msg)} outside {policy.MSG_MIN_CHARS}-{policy.MSG_MAX_CHARS}")
        for rx, label in ((URL_RX, "url"), (EMAIL_RX, "email address"),
                          (PHONE_RX, "phone number"), (LEAK_RX, "prompt or schema leakage"),
                          (PROMISE_RX, "unauthorised promise")):
            if rx.search(msg):
                fatal.append(f"message contains {label}")
        stated = {float(x) for x in MONEY_RX.findall(msg)}
        if stated and stated != {float(amt)}:
            fatal.append(f"message states {stated} but the coupon is ${amt}")
        if _echoes_review(msg, review):
            fatal.append("message echoes a long span of the review verbatim")

    conf = d.get("conf")
    conf = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else 0.0

    decision = {"send": send, "amt": amt, "rc": rc, "why": why, "msg": msg, "conf": conf}
    return decision, v + fatal, bool(fatal)


def safe_default(decision):
    """The fallback applied when a message cannot be repaired: send nothing."""
    return decision | {"send": False, "amt": 0, "msg": ""}
