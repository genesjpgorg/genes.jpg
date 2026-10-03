# Deep-learning architectures for a genome → species-image model (genes.jpg)

Source: Amass API, BiomedCore (PubMed/PMC, including some indexed arXiv/PMLR entries). 20 searches × 20 results, published 2018 or later, retracted papers excluded, about $1 of credits. Items marked **(not in Amass)** come from the author's own knowledge and should be verified before use.

## The pipeline in one line
**DNA encoder** → (optional) **DNA–image alignment** in a shared embedding space → **conditional image generator** (latent diffusion) → evaluation with an image classifier.

The literature does not contain a direct "genome → whole-organism image" generator. The closest published analogues are listed in section 4. The proposed design combines parts that each have published precedent.

---

## 1. DNA encoders (turn sequence into a conditioning vector)

| Model | What it is | Why it matters here | Ref |
|---|---|---|---|
| **ModernGENA** | 22-layer ModernBERT DNA LM (135M params, GENA-LM 32k BPE), pretrained on 443 vertebrate genomes | Chosen encoder; efficient, long context (1024 tokens), ~107 tokens per COI barcode | [AIRI-Institute/moderngena-base](https://huggingface.co/AIRI-Institute/moderngena-base) (not in Amass) |
| **DNACSE** | Contrastive (SimCSE-style) fine-tuning of DNA LMs for barcodes | Reported to beat using the base DNA LMs directly on barcode tasks; improves embedding geometry, which matters for conditioning | J Chem Inf Model 2026, [10.1021/acs.jcim.5c02747](https://doi.org/10.1021/acs.jcim.5c02747) |
| **DNABERT-S** | Species-aware DNA embeddings via contrastive training | Embeddings cluster by species, which is what an image generator needs | ArXiv 2024 (indexed in Amass) |
| **Scorpio** | Contrastive optimisation over DNA LM + k-mer embeddings | Generalises to novel taxa, useful for species unseen in training | Commun Biol 2025, [10.1038/s42003-025-07902-6](https://doi.org/10.1038/s42003-025-07902-6) |
| **HyenaDNA / Caduceus** | Long-context (up to ~1M bp) sub-quadratic DNA models; Caduceus is bidirectional and reverse-complement equivariant | Needed if we move from barcodes to whole genomes or long gene panels | HyenaDNA ArXiv 2023; Caduceus PMLR 2024 |
| **Evo** | 7B long-context genomic foundation model (StripedHyena), trained on prokaryote/phage genomes | Strongest genome-scale model, but trained on prokaryotes and too heavy for CPU; Evo 2 (eukaryotic genomes) **(not in Amass)** | Science 2024, [10.1126/science.ado9336](https://doi.org/10.1126/science.ado9336) |
| **DNABERT-2, Nucleotide Transformer v2, GROVER** | General DNA foundation models | Benchmark: mean-pooled token embeddings work best for classification; general models are mixed on other tasks | Nat Commun 2025 benchmark, [10.1038/s41467-025-65823-8](https://doi.org/10.1038/s41467-025-65823-8) |
| **Chaos Game Representation (CGR) + CNN** | Turns a genome into a 2D image, then uses a CNN | Cheap, alignment-free baseline for whole genomes | Bioinform Adv 2025, [10.1093/bioadv/vbaf193](https://doi.org/10.1093/bioadv/vbaf193); BMC Genomics 2024, [10.1186/s12864-024-11135-y](https://doi.org/10.1186/s12864-024-11135-y) |

**Pick:** ModernGENA (`AIRI-Institute/moderngena-base`, ModernBERT DNA LM; not in Amass), fine-tuned contrastively on COI barcodes. Move to HyenaDNA/Caduceus only for long sequences.

## 2. Aligning DNA and images in one embedding space (contrastive, CLIP-style)

| Model | Pairing | Relevance | Ref |
|---|---|---|---|
| **Difface** | Transformer (SNPs) + spiral CNN (3D faces), aligned contrastively, then a **diffusion decoder** | The closest published "DNA → image" architecture: contrastive alignment, then generation | Adv Sci 2025, [10.1002/advs.202414507](https://doi.org/10.1002/advs.202414507) |
| **CLOOME** | Chemical structure ↔ cell-microscopy images (CLIP with Hopfield retrieval) | Proven non-text modality ↔ bioimage contrastive retrieval | Nat Commun 2023, [10.1038/s41467-023-42328-w](https://doi.org/10.1038/s41467-023-42328-w) |
| **Histology ↔ spatial-transcriptomics contrastive models** (BLEEP-like, GR2ST, DFCE-KanT) | Gene expression ↔ image patches | Many architectures for gene ↔ image alignment | Brief Bioinform 2024, [10.1093/bib/bbae551](https://doi.org/10.1093/bib/bbae551) |
| **CLIBD / BIOSCAN-CLIP** **(not in Amass)** | COI barcode ↔ insect image ↔ taxonomy text | Built on exactly our data (BIOSCAN-5M); gives a ready-made DNA–image space | arXiv/ICLR 2025 |
| **BioCLIP** **(not in Amass)** | Tree-of-life image ↔ taxonomic text (TreeOfLife-10M) | Strong pretrained image encoder for species; also useful for evaluation | CVPR 2024 |

**Pick:** fine-tune a CLIBD-style model, or train one: a ModernGENA DNA tower plus a BioCLIP image tower, with an InfoNCE loss on BIOSCAN-5M pairs. This alone gives **retrieval** ("show the closest real image"), which is a useful baseline before any generation.

## 3. Conditional image generators

| Family | Notes | Bio precedent in Amass |
|---|---|---|
| **Latent diffusion (Stable-Diffusion-like)** with cross-attention on a conditioning embedding | Current state of the art for conditional image generation. Swap the text encoder for the DNA encoder (or its CLIP-aligned projection). Fine-tune only adapters (LoRA / IP-Adapter-style) to save compute | PathLDM (text → histology LDM), WACV 2024, [10.1109/wacv57701.2024.00510](https://doi.org/10.1109/wacv57701.2024.00510); **His-MMDM**, genomics/transcriptomics-guided histology image editing with diffusion, Adv Sci 2026, [10.1002/advs.202518066](https://doi.org/10.1002/advs.202518066); Med-cDiff, 2023, [10.3390/bioengineering10111258](https://doi.org/10.3390/bioengineering10111258) |
| **Transcriptome-conditioned diffusion for cell images** | Gene-expression vector → cell-morphology image. The closest "molecular vector → image" analogue | MorphoDiff, bioRxiv 2024, [10.1101/2024.12.19.629451](https://doi.org/10.1101/2024.12.19.629451); transcriptome-guided diffusion, Nat Commun 2025, [10.1038/s41467-025-63478-z](https://doi.org/10.1038/s41467-025-63478-z) |
| **Conditional style transfer / autoencoder** | Cheaper; edits a base image rather than generating from scratch | IMPA, Nat Commun 2025, [10.1038/s41467-024-55707-8](https://doi.org/10.1038/s41467-024-55707-8) |
| **Conditional GANs** (StackGAN, cGAN) | Fast sampling and trainable on small data, but less diverse and harder to train; mostly superseded | StackGAN++, TPAMI 2018, [10.1109/TPAMI.2018.2856256](https://doi.org/10.1109/TPAMI.2018.2856256) |
| **Multimodal VAEs / mixture-of-experts** | Joint latent space for DNA and image, generating either from the other. Weaker image quality, but works on CPU at low resolution | JAMIE, Nat Mach Intell 2023, [10.1038/s42256-023-00663-z](https://doi.org/10.1038/s42256-023-00663-z); scCross, Genome Biol 2024, [10.1186/s13059-024-03338-z](https://doi.org/10.1186/s13059-024-03338-z) |
| **Cross-attention multimodal diffusion** | Dual cross-attention between modalities in latent diffusion | scDiffusion-X, Nat Commun 2026, [10.1038/s41467-026-71744-x](https://doi.org/10.1038/s41467-026-71744-x) |
| **DiT, ControlNet, flow matching** **(not in Amass)** | Transformer-backbone diffusion; structural control; faster sampling | — |

**Pick:** fine-tune a pretrained latent diffusion model, conditioned through cross-attention on the projected DNA embedding, with classifier-free guidance. Train only adapters at first.

## 4. Closest end-to-end analogues ("genotype → appearance")

- **Difface:** SNPs → 3D human face (contrastive alignment + diffusion). [10.1002/advs.202414507](https://doi.org/10.1002/advs.202414507)
- **His-MMDM:** genomic/transcriptomic profile → edited histology image. [10.1002/advs.202518066](https://doi.org/10.1002/advs.202518066)
- **MorphoDiff / transcriptome-guided diffusion:** expression → cell image.
- **Deep learning on butterfly phenotypes** (wing-pattern embeddings vs. genetics), Sci Adv 2019. [10.1126/sciadv.aaw4967](https://doi.org/10.1126/sciadv.aaw4967)
- **Pathomic Fusion:** histology + genomics fusion, discriminative only, but a common fusion pattern. TMI 2022, [10.1109/TMI.2020.3021387](https://doi.org/10.1109/TMI.2020.3021387)
- **Physical-appearance prediction from DNA** (reviews, Genes 2022/2023/2026): the forensic field, mostly SNP → traits rather than images.

## 5. Evaluation

- **Species accuracy of generated images:** run BioCLIP or a species classifier on the outputs.
- **DNA ↔ image retrieval:** top-k recall in the shared contrastive space.
- **Image quality:** FID / KID per taxonomic level.
- **Generalisation:** hold out species, genera and families (phylogeny-aware split).

## 6. Recommended plan, given CPU-only compute for now

1. **CPU, now:** compute ModernGENA embeddings for a BIOSCAN-5M subset (and BOLD). Build a **retrieval baseline**: nearest real image by DNA embedding, using frozen BioCLIP image embeddings plus a small learned linear projection. This runs on CPU.
2. **Small GPU (when available):** train a CLIBD-style contrastive DNA ↔ image model.
3. **GPU:** fine-tune latent diffusion with a DNA-embedding adapter (IP-Adapter/LoRA), with classifier-free guidance; evaluate with BioCLIP species accuracy and FID.
4. **Scale:** whole-genome conditioning (HyenaDNA/Caduceus, or Evo 2) for species with reference genomes (DToL/EBP), with images joined by taxon ID.

Training diffusion on CPU is not realistic. Steps 1–2 at low resolution are feasible, and a multimodal VAE at 64×64 is a CPU-trainable proof of concept.
