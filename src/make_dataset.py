"""Build the evaluation sample: stratified real reviews plus labelled injections."""
import argparse
import random
import re
import sys

import pandas as pd

import injections

RAW = "data/reviews_raw.csv"
OUT = "data/sample.csv"


def _rating(x):
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 5 else None


def _meta(s):
    """'3 Reviews , 12 Followers' -> (3, 12)"""
    s = str(s or "")
    n = re.search(r"(\d[\d,]*)\s+Review", s)
    f = re.search(r"(\d[\d,]*)\s+Follower", s)
    g = lambda m: int(m.group(1).replace(",", "")) if m else 0
    return g(n), g(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="clean reviews to sample")
    ap.add_argument("--inject", type=int, default=52, help="injected reviews to add")
    ap.add_argument("--seed", type=int, default=429)
    a = ap.parse_args()
    random.seed(a.seed)

    df = pd.read_csv(RAW)
    df["rating"] = df["Rating"].map(_rating)
    df = df.dropna(subset=["Review", "rating"])
    df["review"] = df["Review"].astype(str).str.strip()
    df = df[df["review"].str.len() >= 20]
    df[["n_reviews", "followers"]] = df["Metadata"].apply(lambda s: pd.Series(_meta(s)))

    # Stratify by star rating so low-star recovery cases are not swamped by praise.
    per = max(1, a.n // 5)
    strata = [g.sample(min(per, len(g)), random_state=a.seed)
              for _, g in df.groupby(df["rating"].round())]
    clean = pd.concat(strata).sample(frac=1, random_state=a.seed).reset_index(drop=True)

    rows = []
    for i, r in clean.iterrows():
        rows.append({"id": f"C{i:04d}", "restaurant": r["Restaurant"], "reviewer": r["Reviewer"],
                     "review": r["review"], "rating": r["rating"], "n_reviews": r["n_reviews"],
                     "followers": r["followers"], "is_injection": 0, "injection_family": ""})

    pool = clean.sample(min(a.inject, len(clean)), random_state=a.seed + 1).reset_index(drop=True)
    for i, r in pool.iterrows():
        fam, tpl = injections.PAYLOADS[i % len(injections.PAYLOADS)]
        rows.append({"id": f"X{i:04d}", "restaurant": r["Restaurant"], "reviewer": r["Reviewer"],
                     "review": tpl.format(r=r["review"][:280]), "rating": r["rating"],
                     "n_reviews": r["n_reviews"], "followers": r["followers"],
                     "is_injection": 1, "injection_family": fam})

    out = pd.DataFrame(rows).sample(frac=1, random_state=a.seed + 2).reset_index(drop=True)
    out.to_csv(OUT, index=False)
    print(f"{OUT}: {len(out)} rows ({int(out.is_injection.sum())} injected, "
          f"{len(out) - int(out.is_injection.sum())} clean)")
    print(out.groupby("rating").size().to_string())


if __name__ == "__main__":
    sys.path.insert(0, "src")
    main()
