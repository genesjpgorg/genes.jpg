# Datasets for genome → images and phenotypes (genes.jpg)

> **Status (2026-10-03).** The image side has been measured, not just surveyed: TreeOfLife-200M
> holds 5,537 mammal species (250 of the 270 RefSeq mammal species), TreeOfLife-10M 4,089
> (244), BioTrove 3,725 (256), iNaturalist 2021 246 (86) — see
> [survey_mammals.md](survey_mammals.md). TreeOfLife-200M is the first image source; the
> build pipeline and the first dataset are described in [creating_datasets.md](creating_datasets.md).

Initial literature survey: Amass API, BiomedCore (PubMed/PMC literature), 22 searches, about $1 of credits. Amass indexes **papers that describe datasets**, not the raw genome or image data. Additional resources and access notes below were checked against official documentation, dataset cards, or publisher archives on **2026-10-03**; raw archives were not downloaded. Entries explicitly marked unverified remain leads.

Distinguish **specimen-paired DNA and images**, **species-level joins**, and **gene perturbation evidence**. A DNA barcode is a short identification marker, not a whole genome.

## Tier 1: DNA and images of the *same specimen* (best for direct supervision)

|Dataset|What it pairs|Scale|Paper|
|-|-|-|-|
|**BIOSCAN-5M**|Photograph + DNA barcode of the same specimen; taxonomy, geography and image-size metadata|>5 million specimens, predominantly insects|[Paper](https://arxiv.org/abs/2406.12723); [official repository and downloads](https://github.com/bioscan-ml/BIOSCAN-5M); [Hugging Face](https://huggingface.co/datasets/Gharaee/BIOSCAN-5M)|
|**MassID45**|DNA barcodes + bulk-sample and individual images, with segmentation masks|>17,000 annotated arthropod specimens|*A multi-modal dataset for insect biodiversity with imagery and DNA at the trap and individual level*, Sci Data 2026, PMID 42014735, [doi:10.1038/s41597-026-07251-x](https://doi.org/10.1038/s41597-026-07251-x)|
|**Aphid WGS + photo vouchers**|Whole-genome shotgun sequencing + high-resolution photos of the *same sequenced individuals*|18 species|BMC Genomic Data 2026, PMID 41612194, [doi:10.1186/s12863-026-01406-w](https://doi.org/10.1186/s12863-026-01406-w)|
|**EPT megabarcoding + imaging**|Per-specimen COI barcodes + semi-automated images (size/biomass)|743 specimens, 14 aquatic insect species|PeerJ 2026, PMID 41522517, [doi:10.7717/peerj.20501](https://doi.org/10.7717/peerj.20501)|
|**Darwin Tree of Life genome notes**|Chromosome-level reference genome; each note usually includes a photo of the sequenced specimen (author's knowledge, not checked here)|Thousands of species, growing (plants, insects, mosses…)|Wellcome Open Research "The genome sequence of …" series, e.g. [doi:10.12688/wellcomeopenres.24795.2](https://doi.org/10.12688/wellcomeopenres.24795.2)|
|**Voucher-based barcode libraries**|COI barcodes tied to museum voucher specimens (vouchers often photographed in BOLD)|e.g. Philippine marine fishes; West Sahara-Sahel reptiles|Sci Data 2023 [doi:10.1038/s41597-023-02306-9](https://doi.org/10.1038/s41597-023-02306-9); Sci Data 2022 [doi:10.1038/s41597-022-01582-1](https://doi.org/10.1038/s41597-022-01582-1)|
|**MaizeDIG**|Maize mutant phenotype images linked to genes/genome coordinates (within one species)|Thousands of images|Front Plant Sci 2019, PMID 31555312, [doi:10.3389/fpls.2019.01050](https://doi.org/10.3389/fpls.2019.01050)|

## Tier 2: genome sources (join to images by species / taxon ID)

|Resource|Notes|Paper / access|
|-|-|-|
|**GenBank COI / BOLD**|>2.5M COI barcode sequences in GenBank; BOLD adds specimen metadata and images|PLoS One 2018 [doi:10.1371/journal.pone.0200177](https://doi.org/10.1371/journal.pone.0200177); GigaScience 2022 [doi:10.1093/gigascience/giac123](https://doi.org/10.1093/gigascience/giac123)|
|**Earth BioGenome Project** (incl. ERGA, DToL)|Goal: reference genomes for all eukaryotes; Phase II roadmap|PNAS 2018 [doi:10.1073/pnas.1720115115](https://doi.org/10.1073/pnas.1720115115); Front Sci 2025 [doi:10.3389/fsci.2025.1514835](https://doi.org/10.3389/fsci.2025.1514835)|
|**Genomes on a Tree (GoaT)**|Search engine for genome/assembly metadata across the tree of life. Use it to list which species have genomes|Wellcome Open Res 2023 [doi:10.12688/wellcomeopenres.18658.1](https://doi.org/10.12688/wellcomeopenres.18658.1)|
|**BarcodeBERT pretraining library**|1.5M invertebrate DNA barcodes|Bioinform Adv 2026, PMID 41878470, [doi:10.1093/bioadv/vbag054](https://doi.org/10.1093/bioadv/vbag054)|
|**NCBI Datasets / GenBank / RefSeq assemblies**|Genome sequence, available annotation and assembly/taxon metadata; retrieve by taxon or accession through CLI/API. Pin assembly accession **and version**, and check which assemblies have annotation.|[Genome download guide](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/download-genome/); [taxon CLI reference](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/command-line/datasets/download/genome/datasets_download_genome_taxon/)|
|**Zoonomia**|A defined cohort of **240 placental mammal species**, with assemblies, whole-genome alignment, conservation scores and phylogeny. Candidate genomic side for an AnAge/image join; actual overlap must be measured. The complete HAL alignment is large: start with selected assemblies or loci.|[Official data catalog](https://zoonomiaproject.org/the-data/); [species/assembly table](https://karlssonlab.org/zoonomia/); [paper](https://doi.org/10.1038/s41586-020-2876-6)|
|**TOGA comparative gene annotations**|Orthologous gene annotations and codon alignments across Zoonomia species and additional assemblies; useful for extracting corresponding loci rather than unrelated genome windows. Gene/CDS alignments do not automatically align upstream regulatory regions.|[Official download directory](https://genome.senckenberg.de/download/TOGA/); [Zoonomia access notes](https://zoonomiaproject.org/the-data/)|
|**Ensembl Compara**|Orthology, gene trees and genome alignments for supported species; use orthology type/quality to define a consistent locus panel. Coverage is release-dependent; record the release and retain missing loci explicitly.|[Comparative genomics](https://jun2026.archive.ensembl.org/info/genome/compara/index.html); [homology REST API](https://rest.ensembl.org/documentation/info/homology_species_gene_id)|

## Tier 3: image sources (species-labelled)

|Resource|Scale|Paper|
|-|-|-|
|**iNaturalist**|Very large, global, research-grade species-labelled photos|BioScience 2025 [doi:10.1093/biosci/biaf104](https://doi.org/10.1093/biosci/biaf104)|
|**Austrian Lepidoptera**|540k images, 185 species, expert-verified|Sci Data 2025, PMID 40770239, [doi:10.1038/s41597-025-05708-z](https://doi.org/10.1038/s41597-025-05708-z)|
|**Butterfly phenotype deep learning** (Heliconius wing images)|Wing-pattern images with genetic background|Sci Adv 2019 [doi:10.1126/sciadv.aaw4967](https://doi.org/10.1126/sciadv.aaw4967)|

## Tier 4: quantitative phenotypes (join to genomes/images by species)

These resources provide numeric or categorical targets for separate phenotype heads. They do **not** pair a photographed individual's complete genome with its own lifespan.

|Dataset|Coverage and useful targets|Access / reference|Join and measurement notes|
|-|-|-|-|
|**AnAge**|Curated animal longevity and life history; **4,671 entries in Build 15**, with coverage varying by trait. Maximum longevity, adult mass, maturity, gestation/incubation and some metabolic measurements.|[Database](https://www.genomics.senescence.info/species/index.html); [official tab-delimited download](https://www.genomics.senescence.info/download.html); [field/quality guide](https://www.genomics.senescence.info/help.html); [AnAge paper](https://doi.org/10.1111/j.1420-9101.2009.01783.x)|Start with scientific binomials and reconcile synonyms to an explicit taxon crosswalk. Target **maximum recorded species longevity**, not individual life expectancy. Retain data quality, sample-size category and wild/captive origin; filter questionable records or report sensitivity to their inclusion.|
|**PanTHERIA**|Mammalian life history and ecology: adult body mass, head–body length, gestation, sexual maturity, litter size, activity and maximum longevity.|[Publisher dataset collection](https://figshare.com/collections/PanTHERIA_a_species-level_database_of_life_history_ecology_and_geography_of_extant_and_recently_extinct_mammals/3301274/1); [data dictionary](https://www.esapubs.org/archive/ecol/E090/184/metadata.htm); [paper](https://doi.org/10.1890/08-1494.1)|Two taxonomic versions and synonym tables are supplied. Convert **-999 to missing**. Maximum longevity is in **months**, unlike AnAge's years; respect each field's units and distinguish extrapolated fields from measured summaries.|
|**Amniote life-history database (Myhrvold et al.)**|**21,322 species** of birds, mammals and reptiles have at least one of 29 life-history parameters: body mass, maximum longevity, maturity, clutch/litter size and developmental timings.|[Authors' data page](https://www.weecology.org/data-projects/life-history/); [metadata and files](https://esapubs.org/archive/ecol/E096/269/metadata.php); [paper](https://doi.org/10.1890/15-0846R.1)|Species-level median table, with **-999 null values**. Broad coverage does not mean every species has every trait. Reconcile names/subspecies and retain per-target masks; overlapping literature with other trait databases is not independent validation.|
|**EltonTraits 1.0**|**9,993 birds and 5,400 mammals**; diet composition, foraging stratum/time and body mass. Useful additional phenotype targets and ecological covariates.|[Publisher dataset collection](https://wiley.figshare.com/collections/EltonTraits_1_0_Species-level_foraging_attributes_of_the_world_s_birds_and_mammals/3306933); [metadata](https://esapubs.org/archive/ecol/E095/178/metadata.php); [paper](https://doi.org/10.1890/13-1917.1)|Join through the dataset's scientific-name/taxonomy fields. Preserve source and certainty indicators; separate ecological attributes from visible morphology.|
|**AVONET**|Bird morphology and ecology: **11 continuous morphological traits**, including bill, wing, tail and tarsus measurements, plus body mass and ecological attributes. Raw measurements cover **90,020 individuals from 11,009 species**.|[Author-linked Figshare data](https://figshare.com/s/b990722d72a26b5bfead); [paper and access statement](https://doi.org/10.1111/ele.13898)|Provides individual measurements and species summaries under multiple taxonomies. The individuals measured are not automatically the birds in a separately downloaded photo. Choose one taxonomy and keep measurement units explicit.|

### Gene-level evidence for the lifespan branch

* **GenAge**: curated ageing/longevity-associated genes, including model-organism genetic manipulation evidence and homolog information. Use it to propose a biologically motivated locus panel or audit model explanations; it supplies gene-level evidence, not species lifespan labels or a complete genotype–image dataset. [Database](https://www.genomics.senescence.info/genes/); [downloads](https://www.genomics.senescence.info/download.html); [curation and evidence guide](https://www.genomics.senescence.info/help.html).
* **VertLife phylogenies**: downloadable mammal and bird trees for phylogenetic baselines and defining clade holdouts. These are evaluation resources rather than phenotype targets. [Data portal](https://vertlife.org/data/); [mammal trees](https://vertlife.org/data/mammals/); [mammal tree dataset](https://doi.org/10.5061/dryad.tb03d03).

## Encoders worth reusing (DNA side)

* **modernGENA**: pretrained vertebrate DNA encoder; selected windows need pooling/aggregation for a species-level representation. [Official repository and pretrained models](https://github.com/AIRI-Institute/GENA_LM).
* **BarcodeBERT**: barcode-specific transformer ([doi:10.1093/bioadv/vbag054](https://doi.org/10.1093/bioadv/vbag054))
* **DNABERT-S**: species-aware DNA embeddings ([doi:10.1093/bioinformatics/btaf188](https://doi.org/10.1093/bioinformatics/btaf188))
* **DNACSE**: contrastive fine-tuning for barcodes, reported to beat BarcodeBERT ([doi:10.1021/acs.jcim.5c02747](https://doi.org/10.1021/acs.jcim.5c02747))

## Within-species analogues (genotype → appearance)

* Human face shape GWAS (3D facial images + genotypes): Nat Genet 2018 [doi:10.1038/s41588-018-0057-4](https://doi.org/10.1038/s41588-018-0057-4), Nat Genet 2020 [doi:10.1038/s41588-020-00741-7](https://doi.org/10.1038/s41588-020-00741-7). Access is controlled, and there are re-identification concerns: Sci Adv 2021 [doi:10.1126/sciadv.abg3296](https://doi.org/10.1126/sciadv.abg3296).
* Dog morphology from genomes: PLoS Biol 2010 [doi:10.1371/journal.pbio.1000451](https://doi.org/10.1371/journal.pbio.1000451); Nat Commun 2019 [doi:10.1038/s41467-019-09373-w](https://doi.org/10.1038/s41467-019-09373-w)

## Additional multimodal resources and baselines

* **BIOSCAN-5M access details**: the [official repository](https://github.com/bioscan-ml/BIOSCAN-5M) supplies metadata in CSV/JSON-LD, cropped/resized image packages and the `bioscan-dataset` loader. Join images and barcodes using specimen **`processid`**, and keep its supplied split metadata. Most specimens lack a scientific species label; BINs/placeholder taxa are not scientific names. Its size fields describe pixels/cropping, not calibrated physical body length. Images/metadata are **CC BY 3.0** per the repository.
* **BIOSCAN-1M**: a smaller paired insect resource, useful for an initial subset. Follow the [official repository](https://github.com/bioscan-ml/BIOSCAN-1M) and [dataset record](https://zenodo.org/doi/10.5281/zenodo.8030064). Its specimens are included in BIOSCAN-5M and contribute images to TreeOfLife-10M, so these are not independent test corpora. Its image license is CC BY-NC-SA 4.0, unlike BIOSCAN-5M.
* **TreeOfLife-10M / BioCLIP**: taxonomy-labelled organism images from EOL, iNat21 and BIOSCAN-1M. [Dataset card/downloads](https://huggingface.co/datasets/imageomics/TreeOfLife-10M); [assembly instructions](https://github.com/Imageomics/bioclip/blob/main/docs/imageomics/treeoflife10m.md); [paper](https://arxiv.org/abs/2311.18803). The released EOL component is available on Hugging Face; obtain the other components from their providers to reconstruct the full dataset. A whole-genome join is **species-level**, not specimen-paired. Compilation CC0 does **not** override individual image licenses; retain `licenses.csv`. A BioCLIP-based evaluator shares pretraining data with this corpus, so document overlap when interpreting evaluation.
* **CLIBD**: a DNA–image–taxonomy contrastive model and useful cross-modal retrieval baseline, not a new independent dataset. [Official code and data/checkpoint instructions](https://github.com/bioscan-ml/clibd).
* **Arboretum** remains an image-source lead from the initial survey; verify its current access, provenance and licenses before choosing it.

Related prior art: **G2PDiffusion** already generates morphological images conditioned on DNA barcodes using BIOSCAN-5M. Use it as a relevant baseline/reproduction target; barcode-conditioned image generation does not establish whole-genome or causal phenotype prediction. [Paper](https://arxiv.org/abs/2502.04684).

## Data preparation and evaluation notes

* **Resolve taxonomy before joining.** Keep original names/IDs plus accepted binomial, NCBI taxon ID, assembly accession/version and the evidence for each mapping. HAGR, NCBI, BOLD and image-provider IDs are different namespaces; do not equate them or silently merge ambiguous synonyms.
* **Build separate manifests for paired specimens and species-level joins.** Preserve specimen/image IDs, sequence coordinates/strand, trait source, units, quality, license and split. Report the actual number of species surviving the genome–image–trait intersection.
* **Split by species before fitting or augmentation**, keeping all windows, photos and assemblies for a species together. Add genus/family or phylogenetic holdouts. Thousands of photos/windows for one species do not create thousands of independent lifespan labels.
* **Mask missing targets and preserve provenance.** Convert documented sentinel values, avoid treating imputed traits as independent measurements, and fit preprocessing/imputation on training data only. AnAge, PanTHERIA and the amniote database may share underlying records.
* **Compare with simple baselines.** Use nearest genomic neighbour/taxonomy for images and body mass plus phylogeny for longevity. Keep species names out of the DNA-conditioned rendering prompt and test sequence shuffling within related groups.
* **Retain dataset and per-image attribution.** [HAGR terms](https://www.genomics.senescence.info/legal.html) specify CC BY 3.0; TreeOfLife components have different image terms, including noncommercial/share-alike licenses. Record the exact release and license metadata rather than assuming that public access grants uniform reuse rights.

## Suggested approach

1. For the quickest direct DNA–image experiment, start with a subset of paired barcode data (BIOSCAN-5M, MassID45 or verified BOLD vouchers). Compare a barcode-specialist encoder with modernGENA before selecting one; [modernGENA](https://github.com/AIRI-Institute/GENA_LM) was pretrained on vertebrate assemblies, so insect performance needs validation.
2. For mammalian genome → image + longevity, measure the overlap of **Zoonomia/NCBI assemblies + AnAge/PanTHERIA traits + licensed species photos**. Use TOGA/Ensembl to select corresponding loci; supervision is species-level. Cache frozen encoder embeddings and start with a small learned bridge and separate trait heads.
3. For birds and measured morphology, explore **NCBI/Ensembl sequences + AVONET traits + licensed species photos**.
4. For image → genome, first benchmark retrieval of known genomic candidates using held-out images; evaluate drawings separately because they differ from specimen photographs. Retrieval does not generate a new genome or identify a unique genome from appearance.

