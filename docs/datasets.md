# Datasets for a genome → species-image model (genes.jpg)

Source: Amass API, BiomedCore (PubMed/PMC literature), 22 searches, about $1 of credits. Amass indexes **papers that describe datasets**, not the raw genome or image data, so every item below is the publication you would follow to get the data.

## Tier 1: DNA and images of the *same specimen* (best for direct supervision)

| Dataset | What it pairs | Scale | Paper |
|---|---|---|---|
| **MassID45** | DNA barcodes + bulk-sample and individual images, with segmentation masks | >17,000 annotated arthropod specimens | *A multi-modal dataset for insect biodiversity with imagery and DNA at the trap and individual level*, Sci Data 2026, PMID 42014735, [doi:10.1038/s41597-026-07251-x](https://doi.org/10.1038/s41597-026-07251-x) |
| **Aphid WGS + photo vouchers** | Whole-genome shotgun sequencing + high-resolution photos of the *same sequenced individuals* | 18 species | BMC Genomic Data 2026, PMID 41612194, [doi:10.1186/s12863-026-01406-w](https://doi.org/10.1186/s12863-026-01406-w) |
| **EPT megabarcoding + imaging** | Per-specimen COI barcodes + semi-automated images (size/biomass) | 743 specimens, 14 aquatic insect species | PeerJ 2026, PMID 41522517, [doi:10.7717/peerj.20501](https://doi.org/10.7717/peerj.20501) |
| **Darwin Tree of Life genome notes** | Chromosome-level reference genome; each note usually includes a photo of the sequenced specimen (author's knowledge, not checked here) | Thousands of species, growing (plants, insects, mosses…) | Wellcome Open Research "The genome sequence of …" series, e.g. [doi:10.12688/wellcomeopenres.24795.2](https://doi.org/10.12688/wellcomeopenres.24795.2) |
| **Voucher-based barcode libraries** | COI barcodes tied to museum voucher specimens (vouchers often photographed in BOLD) | e.g. Philippine marine fishes; West Sahara-Sahel reptiles | Sci Data 2023 [doi:10.1038/s41597-023-02306-9](https://doi.org/10.1038/s41597-023-02306-9); Sci Data 2022 [doi:10.1038/s41597-022-01582-1](https://doi.org/10.1038/s41597-022-01582-1) |
| **MaizeDIG** | Maize mutant phenotype images linked to genes/genome coordinates (within one species) | Thousands of images | Front Plant Sci 2019, PMID 31555312, [doi:10.3389/fpls.2019.01050](https://doi.org/10.3389/fpls.2019.01050) |

## Tier 2: genome sources (join to images by species / taxon ID)

| Resource | Notes | Paper |
|---|---|---|
| **GenBank COI / BOLD** | >2.5M COI barcode sequences in GenBank; BOLD adds specimen metadata and images | PLoS One 2018 [doi:10.1371/journal.pone.0200177](https://doi.org/10.1371/journal.pone.0200177); GigaScience 2022 [doi:10.1093/gigascience/giac123](https://doi.org/10.1093/gigascience/giac123) |
| **Earth BioGenome Project** (incl. ERGA, DToL) | Goal: reference genomes for all eukaryotes; Phase II roadmap | PNAS 2018 [doi:10.1073/pnas.1720115115](https://doi.org/10.1073/pnas.1720115115); Front Sci 2025 [doi:10.3389/fsci.2025.1514835](https://doi.org/10.3389/fsci.2025.1514835) |
| **Genomes on a Tree (GoaT)** | Search engine for genome/assembly metadata across the tree of life. Use it to list which species have genomes | Wellcome Open Res 2023 [doi:10.12688/wellcomeopenres.18658.1](https://doi.org/10.12688/wellcomeopenres.18658.1) |

## Tier 3: image sources (species-labelled)

| Resource | Scale | Paper |
|---|---|---|
| **iNaturalist** | Very large, global, research-grade species-labelled photos | BioScience 2025 [doi:10.1093/biosci/biaf104](https://doi.org/10.1093/biosci/biaf104) |
| **Austrian Lepidoptera** | 540k images, 185 species, expert-verified | Sci Data 2025, PMID 40770239, [doi:10.1038/s41597-025-05708-z](https://doi.org/10.1038/s41597-025-05708-z) |
| **Butterfly phenotype deep learning** (Heliconius wing images) | Wing-pattern images with genetic background | Sci Adv 2019 [doi:10.1126/sciadv.aaw4967](https://doi.org/10.1126/sciadv.aaw4967) |

## Encoders worth reusing (DNA side)

- **ModernGENA**: ModernBERT DNA foundation model ([AIRI-Institute/moderngena-base](https://huggingface.co/AIRI-Institute/moderngena-base))
- **DNABERT-S**: species-aware DNA embeddings ([doi:10.1093/bioinformatics/btaf188](https://doi.org/10.1093/bioinformatics/btaf188))
- **DNACSE**: contrastive fine-tuning for barcodes ([doi:10.1021/acs.jcim.5c02747](https://doi.org/10.1021/acs.jcim.5c02747))

## Within-species analogues (genotype → appearance)

- Human face shape GWAS (3D facial images + genotypes): Nat Genet 2018 [doi:10.1038/s41588-018-0057-4](https://doi.org/10.1038/s41588-018-0057-4), Nat Genet 2020 [doi:10.1038/s41588-020-00741-7](https://doi.org/10.1038/s41588-020-00741-7). Access is controlled, and there are re-identification concerns: Sci Adv 2021 [doi:10.1126/sciadv.abg3296](https://doi.org/10.1126/sciadv.abg3296).
- Dog morphology from genomes: PLoS Biol 2010 [doi:10.1371/journal.pbio.1000451](https://doi.org/10.1371/journal.pbio.1000451); Nat Commun 2019 [doi:10.1038/s41467-019-09373-w](https://doi.org/10.1038/s41467-019-09373-w)

## Not in Amass (ML venues / arXiv, not PubMed): from the author's knowledge, verify before use

- **BIOSCAN-1M / BIOSCAN-5M**: millions of insect images, each paired with a COI barcode. This is probably the single best fit for genome→image.
- **CLIBD** (contrastive DNA–image–text model trained on BIOSCAN)
- **TreeOfLife-10M / BioCLIP**, **Arboretum**: large tree-of-life image sets with taxonomy, which can be joined to genomes by taxon.

## Suggested approach
1. Start with paired barcode–image data (BIOSCAN-5M, MassID45, BOLD vouchers). Train a DNA encoder (ModernGENA) → image generator (conditional diffusion).
2. Scale up by joining whole genomes (NCBI/EBP/DToL via GoaT) to iNaturalist/TreeOfLife images on NCBI taxon ID. Supervision is then species-level, not specimen-level.
