"""Coupon policy: the single source of truth for reason codes and amounts.

Keeping the tier table here (not in the prompt's free text) is what makes the
amount decision auditable: the model proposes, this table disposes.
"""

TIERS = [0, 5, 10, 15, 25]          # USD, closed set
MAX_AMOUNT = 25

# reason_code -> (allowed amounts, human label)
REASON_CODES = {
    "HYGIENE_SAFETY":  ([15, 25], "illness, foreign object, pest, or unsafe food handling"),
    "SERVICE_FAILURE": ([5, 10, 15], "rude staff, long wait, wrong or missing order"),
    "FOOD_QUALITY":    ([5, 10, 15], "food cold, stale, undercooked, or badly prepared"),
    "BILLING_ISSUE":   ([5, 10, 15], "overcharge, hidden charge, or refused bill correction"),
    "DELIVERY_ISSUE":  ([5, 10], "late, missing, or damaged delivery"),
    "MIXED_FEEDBACK":  ([0, 5], "broadly positive with a minor, specific complaint"),
    "NO_ISSUE":        ([0], "positive review with no service recovery needed"),
    "UNVERIFIABLE":    ([0], "too vague, empty, or off-topic to act on"),
    "POLICY_BLOCK":    ([0], "blocked by input guardrail (injection or abuse)"),
}

# Ceiling by star rating: a 5-star review cannot buy a 25-dollar coupon.
RATING_CAP = {1: 25, 2: 15, 3: 10, 4: 5, 5: 5}
DEFAULT_CAP = 10                      # rating missing or unparseable

INFLUENCE_FOLLOWERS = 100             # followers at/above which one tier up is allowed

MSG_MIN_CHARS = 60
MSG_MAX_CHARS = 400


def cap_for(rating):
    if rating is None:
        return DEFAULT_CAP
    return RATING_CAP.get(int(round(rating)), DEFAULT_CAP)


def clamp(amount, reason_code, rating, followers=0):
    """Snap a proposed amount to the nearest policy-legal tier at or below the cap.

    Returns (amount, note) where note is None if nothing was changed.
    """
    allowed = REASON_CODES.get(reason_code, ([0], ""))[0]
    cap = cap_for(rating)
    if followers >= INFLUENCE_FOLLOWERS and cap < MAX_AMOUNT:
        cap = TIERS[min(TIERS.index(cap) + 1, len(TIERS) - 1)]
    legal = [t for t in allowed if t <= cap] or [0]
    if amount in legal:
        return amount, None
    snapped = max(t for t in legal if t <= amount) if any(t <= amount for t in legal) else min(legal)
    return snapped, f"amount {amount} not legal for {reason_code} at cap {cap}; snapped to {snapped}"
