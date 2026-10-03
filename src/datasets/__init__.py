"""genes.jpg dataset tooling: build genome <-> species-image datasets.

Layout of a built dataset (root = data/datasets/<name>/, data/datasets is a symlink
to the shared disk):

    dataset.json        DatasetManifest: provenance, schema version, table paths, counts
    species.parquet     one row per species            (SpeciesRecord)
    genomes.parquet     one row per genome assembly    (GenomeRecord)
    images.parquet      one row per downloaded image   (ImageRecord)
    pairs.parquet       image <-> assembly pairing     (PairRecord)
    genomes/<accession>/...                            genome FASTA + NCBI reports
    images/<taxid>/<image_id>.<ext>                    image files
    logs/                                              download failures etc.

See ``datasets.schema`` for the record definitions and ``schemas/dataset.schema.json``
for the exported JSON Schema.
"""

__all__ = ["schema"]
