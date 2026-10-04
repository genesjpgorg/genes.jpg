"""Reproduce the frozen unadjusted 30-hit Spearman screen from committed observations."""

import argparse
import hashlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def test_gene(task):
    gene, data, permutations, base_seed = task
    data = data.sort_values("ensembl_species")
    expression = data.expression_log_tpm.to_numpy()
    lifespan = np.log10(data.max_longevity_yrs.to_numpy())
    yr, lr = stats.rankdata(expression), stats.rankdata(lifespan)
    yr, lr = yr - yr.mean(), lr - lr.mean()
    yr, lr = yr / np.linalg.norm(yr), lr / np.linalg.norm(lr)
    observed = float(yr @ lr)
    seed = base_seed ^ int.from_bytes(hashlib.sha256(gene.encode()).digest()[:8], "little")
    rng = np.random.default_rng(seed)
    count = 0
    for start in range(0, permutations, 5000):
        size = min(5000, permutations - start)
        null = rng.permuted(np.broadcast_to(lr, (size, len(lr))), axis=1) @ yr
        count += int(np.count_nonzero(np.abs(null) >= abs(observed) - 1e-12))
    return {
        "human_gene_id": gene,
        "n_species": len(data),
        "spearman_rho": observed,
        "spearman_p": (count + 1) / (permutations + 1),
        "spearman_extreme_count": count,
        "spearman_asymptotic_p": float(stats.spearmanr(expression, lifespan).pvalue),
    }


def adjust(pvalues, family):
    p = np.asarray(pvalues)
    order = np.argsort(p)
    q = np.empty(len(p))
    q[order] = np.minimum.accumulate((p[order] * family / np.arange(1, len(p) + 1))[::-1])[
        ::-1
    ].clip(0, 1)
    return q


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observations", default="docs/gi-longevity-final/spearman/observations.csv.gz"
    )
    parser.add_argument("--expected", default="docs/gi-longevity-final/spearman/all_genes.csv")
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    data = pd.read_csv(args.observations, float_precision="round_trip")
    tasks = [(gene, frame, 999999, 8192) for gene, frame in data.groupby("human_gene_id")]
    if len(tasks) != 2001 or len(data) != 63906:
        raise ValueError("Input is not the frozen 2,001-gene snapshot")
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(test_gene, tasks, chunksize=4):
            rows.append(row)
            if len(rows) % 100 == 0:
                print(f"Verified screen: {len(rows)}/{len(tasks)} genes", flush=True)
    result = pd.DataFrame(rows).set_index("human_gene_id").sort_index()
    result["spearman_q_bh_planned"] = adjust(result.spearman_p, 3036)
    expected = pd.read_csv(args.expected, index_col="human_gene_id", float_precision="round_trip")
    expected = expected.loc[result.index]
    for field in ["spearman_rho", "spearman_p", "spearman_q_bh_planned"]:
        np.testing.assert_allclose(result[field], expected[field], rtol=1e-12, atol=1e-14)
    np.testing.assert_array_equal(result.spearman_extreme_count, expected.spearman_extreme_count)
    assert int((result.spearman_q_bh_planned < 0.05).sum()) == 30
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output)
    print("Reproduced all 2,001 rho/p/q values and exactly 30 BH-significant genes", flush=True)


if __name__ == "__main__":
    main()
