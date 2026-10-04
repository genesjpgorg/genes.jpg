# TSS Variant Explorer

A local web app that accepts a **human GRCh38 VCF**, applies the selected sample's called variants to the frozen predictor's TSS sequences, infers expression with GI, and reports the percentage change in the model's lifespan estimate relative to matched reference DNA.

This is an experimental extension of a model fitted across mammalian species. It has **not** been validated for differences between human individuals. An output of +4% means a 4% increase in this regression's estimate; it is not a prediction that a person's life will be 4% longer. The model predicts maximum species lifespan, not chronological age or remaining years of life.

## Open the deployed app

**http://localhost:8787**

The service runs in its own **`gi-vcf-web`** tmux session. It survives disconnecting the conversation or closing the browser. The process is bound to `127.0.0.1`, with one web worker and a persistent background job queue.

Deployment on this machine:

| Item | Location |
| --- | --- |
| Code checkout / branch | `/home/fishman/genes-jpg-gi-vcf-service` / `gi-vcf-web` |
| Private runtime state | `/home/fishman/.local/share/gi-lifespan-web` |
| Indexed reference | `GRCh38.fa` and `GRCh38.fa.fai` in runtime state |
| Server log | `server.log` in runtime state |
| API key source | `/home/fishman/genes.jpg/.env`, read on server startup |
| tmux session | `gi-vcf-web` |

If the browser is on a different computer, tunnel the server's loopback port:

```bash
ssh -N -L 8787:127.0.0.1:8787 user@server
```

Then open `http://localhost:8787` on that computer. Do not change the server binding to expose this personal workspace publicly: it has no user accounts or authentication.

## Use the app

1. Upload `.vcf` or `.vcf.gz` (ordinary gzip or BGZF; no index needed), at most 512 MB. A VCF with sample genotype calls is required; sites-only VCFs are rejected.
2. Select the sample when there is more than one, and confirm GRCh38/hg38. Declared GRCh37/hg19 inputs are rejected. No liftover is performed.
3. Choose **Selected genes** (default) or **All genes**. The latter can require thousands of GI calls for a whole genome.
4. Leave phase draws at 4, or choose 2 / 8. Click **Run analysis**.
5. Bookmark the resulting `?job=...` URL. The app shows progress, the percentage change versus reference, and each affected gene's expression and contribution.
6. Download the full result JSON or the gene-effects CSV. A synthetic ABHD4 example is available on the page; it does not represent a real individual.

Uploads and assembled sequences stay in the local runtime state directory. Relevant native and modified DNA windows are sent to the GI API. The server reads the key; the browser never receives it. Uploaded data and GI caches are retained until you remove them locally. They are outside the Git checkout and are not committed.

## Models and reference

The app reuses the exported weights and preprocessing parameters without refitting:

| Model ID | Fitted genes | Available human windows |
| --- | ---: | ---: |
| `fdr_genes_ridge` | 57 | 56 |
| `all_genes_ridge` | 3,036 | 3,034 |

The human-missing feature `ENSG00000170464` is median-imputed in both models; `ENSG00000266200` is also imputed in the all-gene model. Variants in genes without a reference window cannot affect this app's prediction. The denominator is the model’s matched reference prediction. The interface, API and downloads report percentage changes without absolute age or lifespan estimates.

See the [frozen model manual](../gi-longevity-final/USAGE.md), [methodology](../gi-longevity-final/METHODOLOGY.md), [coefficients](../gi-lifespan-predictor/coefficients.csv), and [reference provenance](../gi-longevity-final/reference_human_provenance.json).

## Variant-to-expression method

- Use the pinned Ensembl 116 GRCh38 primary assembly, original annotated transcript/TSS, and strand from `reference_human_tss.csv`. Every used native sequence must match its saved SHA-256. The deployed reference was checked against **all 3,034** saved windows.
- GI receives **81,920 bp**, with the TSS at zero-based index **40,960**. The input sequence is larger than the model's central expression-scoring region; variants throughout the input are considered. These windows are not the smaller central ±4,096-bp region used in the earlier shuffle experiment.
- Scan all records; keep those intersecting a fitted gene's input window plus a 2,048-bp assembly flank. A variant may affect multiple genes. Small deletions may pull flanking sequence into the final input. Flank-only edits that do not change the final window cause no inference.
- Accept `FILTER=PASS` or `.`, and `FORMAT/FT=PASS` or `.` if present. Other filters and missing/partial GT calls are counted and skipped. An absent or skipped call retains reference sequence; **this does not establish that the site was callable**. No extra QUAL/GQ threshold is imposed.
- Interpret the chosen sample's haploid or diploid GT, including multiallelic `1/2`. `0/0` causes no edits. Haploid calls are duplicated for the expression averaging rule; copy-number effects are not modeled. Normalize `chr1` to `1`, and `chrM`/`M` to `MT`. Validate the full VCF REF against plus-strand GRCh38 before applying a non-reference allele.
- Support sequence-resolved SNVs, MNVs, and small indels with each used REF/ALT at most 1,000 bp. Reject relevant called symbolic alleles, breakends, spanning-deletion `*` alleles, and larger alleles. `INFO/END` is used when checking interval overlap. No structural-variant reconstruction is attempted.
- Trim shared REF/ALT prefix and suffix, apply alleles on the genomic plus strand, and track the surviving reference TSS through indels. Crop exactly 81,920 bases, then reverse-complement negative-strand genes. Reject incompatible overlaps within a haplotype, indels that remove/ambiguously replace the TSS, exhausted assembly flanks, or sequence QC failures. TSS switching is not modeled.
- Preserve phased `GT` within each chromosome/`PS` block. Phased calls without PS share an implicit block on that chromosome. Each unphased heterozygote is its own block. Reproducibly flip block orientation for each phase draw using SHA-256 and seed `20261004`; this samples unknown relative phase between blocks. It does not enumerate all possible haplotypes. Two, four, or eight draws are available.
- For each draw, infer both haplotypes and average their **TPM**, then average TPM over draws. Convert back to `log(1+TPM)` for the regressor. This is a modeling assumption about diploid aggregation; it is not an experimentally validated dosage model.

The API payload always uses:

```json
{
  "model": "g0-expression-8192",
  "sequence_name": "orthologue_tss_window",
  "tss_index": 40960,
  "options": {
    "description": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq."
  },
  "sequence": "<81920 bases>"
}
```

No sample name, variant label, species label change, or other context change is inserted into that payload. The sequence is the changing input. [GI task documentation](https://docs.genomicintelligence.ai/tasks) and the [VCF specification](https://samtools.github.io/hts-specs/VCFv4.3.pdf) describe the underlying interfaces.

## Percentage calculation and matched controls

For each affected gene, the app also infers its **native reference DNA**, using the same fixed GI configuration and service cache as the modified DNA. Unaffected genes retain their archived reference expression. This yields a matched reference vector and a modified vector. Native requests may be served from the content-addressed service cache; timestamps/raw responses are retained locally. The live GI service can change over time, and its model ID alone cannot guarantee permanent numerical reproducibility; retain the responses for a reproducible report.

Let `x_g` be expression in `log(1+TPM)`, `s_g` the training scale, and `beta_g` the standardized coefficient. The intercept and training means cancel in the comparison:

```text
gene_log_contribution = beta_g * (x_modified_g - x_reference_g) / s_g
log_ratio = sum_g gene_log_contribution
percent_change = 100 * expm1(log_ratio)
```

Missing human features use the saved training median. Contributions sum in **log-lifespan units**, not percentage units. Only the percentage change versus the matched reference is reported. Absolute estimates are omitted from the UI, configuration API, job API, and downloadable JSON, including when reopening older saved jobs. The min/max result over phase draws describes sensitivity to phase assignments; it is **not a confidence interval**. With no window-changing calls, the answer is exactly 0% and no GI request is made.

## Install and start elsewhere

From a checkout containing the committed model artifacts:

```bash
uv sync --extra analysis --extra web

# Download the exact reference only if it is not already available.
curl -fL --retry 3 \
  https://ftp.ensembl.org/pub/release-116/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz \
  -o /path/to/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz

.venv/bin/python scripts/prepare_gi_web_reference.py \
  /path/to/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz \
  "$HOME/.local/share/gi-lifespan-web/GRCh38.fa"

# .env contains GI_API_KEY=...; do not put the key in a shell argument.
export GI_ENV_FILE=/path/to/.env
./scripts/run_gi_vcf_web.sh
```

Reference preparation verifies the compressed FASTA SHA-256, decompresses it, and creates a pysam FASTA index. Allow roughly 3.2 GB for the uncompressed reference, plus input and sequence-cache storage. Startup also checks that the frozen weights reproduce both saved human predictions.

Configuration:

| Variable | Default |
| --- | --- |
| `GI_ENV_FILE` | `.env` at checkout root |
| `GI_API_KEY` | If set in the environment, overrides the env file |
| `GI_WEB_STATE` | `$HOME/.local/share/gi-lifespan-web` |
| `GI_REFERENCE_FASTA` | `$GI_WEB_STATE/GRCh38.fa` when using the start script |
| `GI_WEB_PORT` | `8787` |

To restart this deployment after stopping it, from its checkout:

```bash
# Run only when the gi-vcf-web session has stopped.
tmux new-session -d -s gi-vcf-web \
  -c /home/fishman/genes-jpg-gi-vcf-service \
  'GI_ENV_FILE=/home/fishman/genes.jpg/.env ./scripts/run_gi_vcf_web.sh >> /home/fishman/.local/share/gi-lifespan-web/server.log 2>&1'

tmux attach -t gi-vcf-web
# Ctrl-B, then D detaches. Ctrl-C stops the server.
```

Jobs run one at a time. GI uses at most four concurrent calls, initially limited to 1.5 requests/sec and reduced if rate-limit headers require it. Identical payloads are deduplicated across haplotypes, genes, draws, and jobs. Failed/cancelled jobs can be rerun from the UI with the same upload; successful API calls remain cached. On restart, queued/interrupted jobs are automatically reconstructed and resumed from cache. In-flight requests may finish before cancellation or shutdown completes. The process uses a state-directory lock; do not run multiple workers against one state directory.

## API and artifacts for reports/agents

OpenAPI schema: `/openapi.json`. All APIs are on the same loopback origin.

| Route | Purpose |
| --- | --- |
| `GET /api/config` | Reference/key readiness and frozen models |
| `POST /api/uploads` | Multipart `file`; returns upload `id`, samples and assembly evidence |
| `POST /api/jobs` | Start a background job; JSON below |
| `GET /api/jobs/{id}` | Progress, counters, final result or actionable error |
| `POST /api/jobs/{id}/cancel` | Cancel queued or active work |
| `GET /api/jobs/{id}/result.json` | Download full reproducible result metadata |
| `GET /api/jobs/{id}/genes.csv` | Download affected-gene expression/effect table |
| `GET /api/example.vcf` | Synthetic homozygous ABHD4 SNV for an end-to-end smoke test |

```json
{
  "upload_id": "<UUID returned by upload>",
  "sample": "<exact VCF sample name>",
  "model": "fdr_genes_ridge",
  "phase_draws": 4,
  "assembly": "GRCh38"
}
```

Browser uploads and `curl -F file=@sample.vcf` supply the required `Content-Length`; chunked uploads without a known length are rejected. Expanded VCFs are limited to 4 GB, 20 million records, and 4 MB per line. Known wrong assemblies, malformed calls, REF mismatches, unsupported relevant variants, and inconsistent TSS windows fail explicitly instead of returning a partial variant prediction.

Runtime artifact inventory (under `GI_WEB_STATE`):

| Path | Contents and use |
| --- | --- |
| `uploads/{id}/input.vcf` | Original uploaded bytes; may be gzip despite the stored suffix |
| `uploads/{id}/metadata.json` | Original filename, samples, build evidence, byte count and input SHA-256 |
| `jobs/{id}/status.json` | Job options, progress, counters, timestamps, and completed result |
| `jobs/{id}/result.json` | Report source: percentage change versus reference, model/context, phase range, per-gene effects, input/model hashes, assumptions |
| `jobs/{id}/sequence_plan.json` | Each affected gene's native request hash and two haplotype hashes per phase draw; also embedded in result JSON |
| `jobs/{id}/sequences.sqlite` | Deduplicated zlib-compressed DNA keyed by exact GI request hash; private genomic data |
| `jobs/{id}/expressions.json` | Request hash → validated GI `log(1+TPM)` expression |
| `responses/{request_hash}.json` | Raw validated GI response and server metadata; exact-payload cache |
| `GRCh38.provenance.json` | Preparation source hash and assembly/release |
| `server.log` | Local service and HTTP progress log; API key is not logged |

`result_schema_version` is 2. The web reporting layer omits the three absolute `*_predicted_years` fields from schema 1; its percentage calculation is unchanged. Older files retained privately in runtime storage are filtered to schema 2 when served. `api_requests` counts newly successful GI inferences, not retry HTTP attempts; `cache_hits` counts reused responses. `requests_total` counts unique sequence payloads. Gene-level `variant_records` counts retained records in the padded window; some padding records may not affect the cropped sequence. `genes_affected` counts genes whose final DNA differs in at least one sampled haplotype, whether or not GI predicts an expression difference. Filter counters apply to padded predictor windows; `outside_windows` counts discarded records elsewhere.

For reports, take numeric values directly from JSON/CSV. Identify the model, human assembly, fixed context, phase draws, reference-control policy, and experimental comparative-model interpretation. Do not substitute an observed human lifespan into the denominator or describe phase ranges as uncertainty intervals.

Source files:

- [`longevity/gi_vcf.py`](../../longevity/gi_vcf.py): VCF filtering, phasing, sequence edits, centering, frozen predictor and comparison math.
- [`longevity/gi_web.py`](../../longevity/gi_web.py): upload API, persistent queue, GI inference/cache, results and local service.
- [`longevity/web/`](../../longevity/web/): standalone HTML/CSS/JavaScript interface.
- [`scripts/prepare_gi_web_reference.py`](../../scripts/prepare_gi_web_reference.py) and [`scripts/run_gi_vcf_web.sh`](../../scripts/run_gi_vcf_web.sh): setup and deployment.
- [`tests/test_gi_vcf.py`](../../tests/test_gi_vcf.py) and [`tests/test_gi_web.py`](../../tests/test_gi_web.py): scientific and mocked-API integration tests.

Run the relevant tests:

```bash
.venv/bin/pytest tests/test_gi_expression.py tests/test_gi_lifespan_predictor.py \
  tests/test_gi_sequence_lifespan.py tests/test_gi_tss_shuffle.py \
  tests/test_gi_vcf.py tests/test_gi_web.py -q
```

Deployment was additionally checked in Chromium at desktop and 390-pixel mobile widths: upload, sample selection, live GI inference, restored job URL, CSV/JSON downloads, and no browser JavaScript errors. The synthetic homozygous ABHD4 example returned **+4.146933% versus reference** with two live GI calls. This is a software/inference smoke test, not a biological validation of that variant.

Committed smoke-test artifacts:

| Artifact | Use |
| --- | --- |
| [`synthetic_ABHD4.vcf`](synthetic_ABHD4.vcf) | Exact synthetic GRCh38 input; no personal sample data |
| [`synthetic_result.json`](synthetic_result.json) | Selected-gene model: +4.146933%, including sequence request hashes and artifact checksums |
| [`synthetic_result_all_genes.json`](synthetic_result_all_genes.json) | All-gene model: +0.363800% versus reference; reused the same two GI sequence results |

The final targeted suite passed **37 tests**, including cancellation without a partial prediction and automatic restart from cached inference. Formatting/lint checks passed for the added Python modules and tests. The existing Starlette test client emits a deprecation warning for its httpx transport; this does not affect the running server or test results.
