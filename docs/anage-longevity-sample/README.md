# anage-longevity-sample v0.1.0

This is the checked-in README snapshot for `data/datasets/anage-longevity-sample/`.
The dataset files are stored separately and are not included in Git. Paths below are
relative to that dataset directory. The longevity builder code is not included in this
documentation-only commit.

5-genome sample of the genome <-> longevity dataset (AnAge maximum longevity records paired with NCBI reference assemblies): two mammals reused from the genome <-> image datasets, the naked mole-rat, baker's yeast and one GenBank-only fish.

Built 2026-10-03 by genes.jpg `datasets.longevity` (schema 0.1.0): 5 species, 5 genome assemblies (4 RefSeq, 1 GenBank-only; 2.6 GB of gzipped FASTA), 5 AnAge longevity records (5 primary).

## How this dataset was created

1. **Longevity.** AnAge Build 15 (July 3, 2023) was downloaded from <https://genomics.senescence.info/species/dataset.zip>. AnAge gives one maximum-longevity record per species (`max_longevity_yrs`) with its origin (`specimen_origin`: wild / captivity / unknown), a `data_quality` rating and a `sample_size` class, plus life-history traits (body mass, maturity, gestation, litter size, metabolic rate, IMR, MRDT).
2. **Species.** Every AnAge `Genus species` was resolved in NCBI Taxonomy (synonyms to the accepted name, subspecies lifted to the species, spelling variants via NCBI's name suggestions). Identity is the species-rank taxid.
3. **Genomes.** NCBI `assembly_summary` snapshots (RefSeq and GenBank; mammals, other vertebrates, invertebrates, plants, fungi) were indexed by species taxid, so an assembly of a subspecies or breed counts for its species. One assembly per species: plausible genome size for its class, RefSeq over GenBank, primary over alternate pseudohaplotype, no NCBI exclusion reason, the exact taxon AnAge named, then reference > representative, Complete Genome > Chromosome > Scaffold > Contig, newest.
4. **Files.** `<accession>_genomic.fna.gz` (all top-level sequences, soft-masked, as distributed by NCBI), `<accession>_assembly_report.txt` and `md5checksums.txt` per assembly, every FASTA verified against NCBI's MD5. Files already present in other genes.jpg datasets were hard-linked instead of downloaded (1 from `tol200m-mammals-smoke`, 1 from `tol200m-refseq-diverse`).
5. **Pairing.** `longevity.ncbi_taxid` -> `species.ncbi_taxid`, `longevity.assembly_accession` -> `genomes.assembly_accession`. The sequenced individual is never the one whose longevity was recorded: this is species-level supervision. When several AnAge rows map to one species (e.g. dog and wolf both to *Canis lupus*) all are kept and exactly one is `is_primary`.

See `dataset.json` (provenance, selection parameters, per-class longevity summary), `logs/build_report.json` (what was linked, downloaded or failed) for build provenance and results.

## Layout

```
anage-longevity-sample/
  dataset.json           manifest: sources, tables, counts, selection
  species.parquet/.csv   one row per species (NCBI lineage, AnAge names)
  genomes.parquet/.csv   one row per assembly file (accession, size, MD5, paths)
  longevity.parquet/.csv one row per AnAge record (longevity + life-history traits)
  genomes/<accession>/<accession>_<asm>_genomic.fna.gz, _assembly_report.txt, md5checksums.txt
  logs/                  build_report.json, genome_failures.jsonl
```

## What it includes

| class | species | genomes | RefSeq | GenBank | longevity min / median / max (yrs) |
|---|---|---|---|---|---|
| Mammalia | 3 | 3 | 3 | 0 | 14.8 / 21.3 / 31 |
| Actinopteri | 1 | 1 | 0 | 1 | 5 / 5 / 5 |
| Saccharomycetes | 1 | 1 | 1 | 0 | 0.04 / 0.04 / 0.04 |

Assembly levels: Chromosome 3, Complete Genome 1, Scaffold 1.
Longevity record origin: captivity 4, wild 1.
Data quality: acceptable 4, high 1.

## Species

| taxid | species | AnAge name | assembly | level | genome size (bp) | longevity (yrs) | origin |
|---|---|---|---|---|---|---|---|
| 9627 | Vulpes vulpes | Vulpes vulpes | GCF_048418805.1 VulVul3 | Chromosome | 2,395,584,468 | 21.3 | captivity |
| 10181 | Heterocephalus glaber | Heterocephalus glaber | GCF_053883595.1 H.glaber_assembly_v1.0 | Scaffold | 2,561,259,134 | 31 | captivity |
| 55149 | Sciurus vulgaris | Sciurus vulgaris | GCF_902686455.1 mSciVul1.2 | Chromosome | 2,878,591,032 | 14.8 | captivity |
| 240832 | Thaleichthys pacificus | Thaleichthys pacificus | GCA_023658055.1 Tpac_2.0 | Chromosome | 468,759,031 | 5 | wild |
| 4932 | Saccharomyces cerevisiae | Saccharomyces cerevisiae | GCF_000146045.2 R64 | Complete Genome | 12,071,326 | 0.04 | captivity |

## Caveats

- AnAge longevity is a single maximum record, frequently from captivity and often from small samples; use `specimen_origin`, `sample_size` and `data_quality` as filters or covariates.
- GenBank-only assemblies vary in quality (see `assembly_level`, `scaffold_count`, `contig_count`); `genomes.source == "ncbi_refseq"` selects the RefSeq subset.
- Species-level pairing: a genome is a reference for the species, not the individual.

## Sources

- AnAge (Human Ageing Genomic Resources) @ Build 15 (July 3, 2023) <https://genomics.senescence.info/species/dataset.zip> - Free to use subject to the HAGR conditions (https://genomics.senescence.info/legal.html); cite Tacutu et al. 2013 NAR 41:D1027 and de Magalhaes & Costa 2009 J Evol Biol 22:1770.
- NCBI Assembly (RefSeq and GenBank) @ assembly_summary snapshots of assembly_summary_fungi_genbank.txt, assembly_summary_fungi_refseq.txt, assembly_summary_invertebrate_genbank.txt, assembly_summary_invertebrate_refseq.txt, assembly_summary_plant_genbank.txt, assembly_summary_plant_refseq.txt, assembly_summary_vertebrate_mammalian_genbank.txt, assembly_summary_vertebrate_mammalian_refseq.txt, assembly_summary_vertebrate_other_genbank.txt, assembly_summary_vertebrate_other_refseq.txt (2026-10-03) <https://www.ncbi.nlm.nih.gov/datasets/> - NCBI data: public domain / no restrictions
