"""Build the evaluation sample entirely from real reviews: the ~42 injection
payloads a previous full-dataset scan found already planted in the raw CSV,
plus a stratified sample of genuinely clean reviews. No synthetic injections -
every row here is something a real reviewer (or whoever seeded the dataset)
actually wrote.
"""
import re
import sys

import pandas as pd

sys.path.insert(0, "src")
import guardrails as g

RAW = "data/reviews_raw.csv"
OUT = "data/sample.csv"
SEED = 429

# Manually verified against the full 10000-row scan (see conversation notes):
# every index here contains a genuine, deliberately crafted injection payload,
# confirmed by reading the full review text, not just a regex hit.
TRUE_INJECTION_IDX = [
    388, 507, 677, 716, 1333, 1339, 1493, 1672, 1849, 1926, 1938, 2195, 2265,
    2563, 3024, 3450, 3471, 3510, 3975, 4034, 4094, 4664, 4694, 5021, 5026,
    5518, 5633, 5971, 6079, 6831, 6938, 7465, 7733, 7746, 8304, 8648, 8816,
    9286, 9346, 9347, 9619, 9733,
]
# Regex hits that a manual read showed were NOT attacks (menu items, ordinary
# narrative mentioning a real discount, promo links, an email signature).
# Excluded from the clean pool too, so the clean/injection split has no
# ambiguous rows either way.
CONFOUNDED_IDX = [
    240, 371, 809, 861, 1282, 1522, 1564, 1705, 2337, 2597, 3030, 3386, 3831,
    5443, 6142, 6352, 6794, 7547, 8800, 9169, 9302, 9807, 9772,
]


def _rating(x):
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 5 else None


def _meta(s):
    s = str(s or "")
    n = re.search(r"(\d[\d,]*)\s+Review", s)
    f = re.search(r"(\d[\d,]*)\s+Follower", s)
    grab = lambda m: int(m.group(1).replace(",", "")) if m else 0
    return grab(n), grab(f)


def main(n_clean=200):
    df = pd.read_csv(RAW)
    df["rating"] = df["Rating"].map(_rating)
    df["review"] = df["Review"].astype(str).str.strip()
    df[["n_reviews", "followers"]] = df["Metadata"].apply(lambda s: pd.Series(_meta(s)))

    rows = []
    for idx in TRUE_INJECTION_IDX:
        r = df.loc[idx]
        _, fams = g.screen_input(r["review"])
        rows.append({"id": f"X{idx:05d}", "restaurant": r["Restaurant"], "reviewer": r["Reviewer"],
                     "review": r["review"], "rating": r["rating"], "n_reviews": r["n_reviews"],
                     "followers": r["followers"], "is_injection": 1,
                     "injection_family": ",".join(fams) or "unclassified"})

    pool = df.dropna(subset=["review", "rating"])
    pool = pool[pool["review"].str.len() >= 20]
    pool = pool.drop(index=[i for i in TRUE_INJECTION_IDX + CONFOUNDED_IDX if i in pool.index])

    per = max(1, n_clean // 5)
    strata = [grp.sample(min(per, len(grp)), random_state=SEED)
              for _, grp in pool.groupby(pool["rating"].round())]
    clean = pd.concat(strata).sample(frac=1, random_state=SEED)
    for idx, r in clean.iterrows():
        rows.append({"id": f"C{idx:05d}", "restaurant": r["Restaurant"], "reviewer": r["Reviewer"],
                     "review": r["review"], "rating": r["rating"], "n_reviews": r["n_reviews"],
                     "followers": r["followers"], "is_injection": 0, "injection_family": ""})

    out = pd.DataFrame(rows).sample(frac=1, random_state=SEED + 1).reset_index(drop=True)
    out.to_csv(OUT, index=False)
    print(f"{OUT}: {len(out)} rows ({int(out.is_injection.sum())} real planted injections, "
          f"{len(out) - int(out.is_injection.sum())} clean, all from the real 10000-row CSV)")
    print(out.groupby("rating").size().to_string())


if __name__ == "__main__":
    main()
