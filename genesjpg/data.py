"""BIOSCAN-5M subset download and paired (DNA, image, taxonomy) dataset.

Only the needed bytes are fetched: the metadata CSV member is streamed out of its
zip with one HTTP range request, and images are fetched in contiguous blocks of
zip entries, so a few thousand specimens cost ~100 MB instead of the full archives.
"""

from __future__ import annotations

import csv
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import requests
from remotezip import RemoteZip

HF_BASE = "https://huggingface.co/datasets/bioscan-ml/BIOSCAN-5M/resolve/main/"
METADATA_ZIP = "BIOSCAN_5M_Insect_Dataset_metadata_MultiTypes.zip"
METADATA_MEMBER = "bioscan5m/metadata/csv/BIOSCAN_5M_Insect_Dataset_metadata.csv"
IMAGE_ZIPS = {
    "train": "BIOSCAN_5M_cropped_256_train.zip",
    "eval": "BIOSCAN_5M_cropped_256_eval.zip",
}
TAXONOMY = ["order", "family", "subfamily", "genus", "species"]
FIELDS = ["processid", "split", "dna_barcode", "dna_bin", *TAXONOMY, "image_path"]


@dataclass
class Entry:
    """Location of one member inside a remote zip archive."""

    name: str
    offset: int
    compress_size: int
    compress_type: int


def _list_split(zip_name: str, split: str) -> list[Entry]:
    """List the JPEGs of one split from the remote zip's central directory, sorted by file offset."""
    prefix = f"bioscan5m/images/cropped_256/{split}/"
    with RemoteZip(HF_BASE + zip_name) as z:
        out = [
            Entry(i.filename, i.header_offset, i.compress_size, i.compress_type)
            for i in z.infolist()
            if i.filename.startswith(prefix) and i.filename.endswith(".jpg")
        ]
    return sorted(out, key=lambda e: e.offset)


def _select_blocks(entries: list[Entry], n: int, n_blocks: int) -> list[list[Entry]]:
    """Pick n entries as n_blocks contiguous runs spread evenly over the archive."""
    n = min(n, len(entries))
    n_blocks = max(1, min(n_blocks, n))
    per = n // n_blocks
    stride = len(entries) // n_blocks
    return [entries[b * stride : b * stride + per] for b in range(n_blocks)]


def _range(url: str, start: int, end: int) -> bytes:
    """Fetch bytes [start, end) of a URL with an HTTP Range request."""
    r = requests.get(url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=600)
    r.raise_for_status()
    return r.content


def _extract_block(url: str, block: list[Entry]) -> dict[str, bytes]:
    """Download a contiguous run of zip entries in one request and decompress each member."""
    start = block[0].offset
    last = block[-1]
    end = last.offset + 30 + 1024 + last.compress_size  # local header + generous name/extra slack
    buf = _range(url, start, end)
    files = {}
    for e in block:
        p = e.offset - start
        sig, *_rest = struct.unpack("<I", buf[p : p + 4])
        if sig != 0x04034B50:
            raise ValueError(f"bad local header for {e.name}")
        name_len, extra_len = struct.unpack("<HH", buf[p + 26 : p + 30])
        data_start = p + 30 + name_len + extra_len
        raw = buf[data_start : data_start + e.compress_size]
        files[e.name] = raw if e.compress_type == 0 else zlib.decompress(raw, -15)
    return files


def _stream_metadata(wanted: set[str]) -> dict[str, dict]:
    """Stream-decompress the metadata CSV out of its zip and keep only rows whose processid is in ``wanted``."""
    url = HF_BASE + METADATA_ZIP
    with RemoteZip(url) as z:
        info = z.getinfo(METADATA_MEMBER)
    head = _range(url, info.header_offset, info.header_offset + 30)
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    start = info.header_offset + 30 + name_len + extra_len
    rows: dict[str, dict] = {}
    dec = zlib.decompressobj(-15)
    pending = b""
    header: list[str] | None = None
    with requests.get(
        url,
        headers={"Range": f"bytes={start}-{start + info.compress_size - 1}"},
        stream=True,
        timeout=600,
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            pending += dec.decompress(chunk)
            *lines, pending = pending.split(b"\n")
            for line in lines:
                rec = next(csv.reader([line.decode("utf-8")]))
                if header is None:
                    header = rec
                    continue
                row = dict(zip(header, rec))
                if row.get("processid") in wanted:
                    rows[row["processid"]] = row
            if len(rows) == len(wanted):
                break
    return rows


def download_subset(
    out_dir: str | Path, n_train: int = 20000, n_eval: int = 3000, n_blocks: int = 20
) -> Path:
    """Download a paired BIOSCAN-5M subset; returns the path of the records CSV.

    train comes from the `train` split, eval from `val` (seen species) and
    `val_unseen` (species held out of training).
    """
    out = Path(out_dir)
    plan = [("train", "train", n_train), ("eval", "val", n_eval), ("eval", "val_unseen", n_eval)]
    selected: dict[str, tuple[str, list[list[Entry]]]] = {}
    for zip_key, split, n in plan:
        entries = _list_split(IMAGE_ZIPS[zip_key], split)
        selected[split] = (IMAGE_ZIPS[zip_key], _select_blocks(entries, n, n_blocks))
        print(f"{split}: {sum(map(len, selected[split][1]))} of {len(entries)} images selected")

    wanted = {Path(e.name).stem for _, blocks in selected.values() for b in blocks for e in b}
    meta = _stream_metadata(wanted)
    print(f"metadata rows matched: {len(meta)}/{len(wanted)}")

    records = []
    for split, (zip_name, blocks) in selected.items():
        img_dir = out / "images" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        for block in blocks:
            for name, data in _extract_block(HF_BASE + zip_name, block).items():
                pid = Path(name).stem
                row = meta.get(pid)
                if not row or not row.get("dna_barcode") or not row.get("genus"):
                    continue
                path = img_dir / f"{pid}.jpg"
                path.write_bytes(data)
                rec = {k: row.get(k, "") for k in FIELDS}
                rec.update(split=split, image_path=str(path.relative_to(out)))
                records.append(rec)
    csv_path = out / "records.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)
    print(f"wrote {len(records)} records to {csv_path}")
    return csv_path


def read_records(csv_path: str | Path, split: str | None = None) -> list[dict]:
    """Read the records CSV written by ``download_subset``, optionally filtered to one split."""
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if split is None or r["split"] == split]


def taxonomy_text(r: dict) -> str:
    """Taxonomic caption in BioCLIP's 'a photo of <taxonomy>' style."""
    names = [r.get(k, "").strip() for k in TAXONOMY]
    names = [n for n in names if n and n.lower() != "not_classified"]
    return "a photo of " + " ".join(["Animalia Arthropoda Insecta", *names])


def label(r: dict) -> str:
    """Finest available label: species, else genus."""
    return r.get("species") or f"{r['genus']} sp."


def synthetic_records(
    n: int = 64, n_species: int = 8, seq_len: int = 658, seed: int = 0
) -> list[dict]:
    """Random barcode records with species-specific sequences, for tests."""
    import random

    rng = random.Random(seed)
    protos = ["".join(rng.choice("ACGT") for _ in range(seq_len)) for _ in range(n_species)]
    recs = []
    for i in range(n):
        s = i % n_species
        seq = list(protos[s])
        for j in rng.sample(range(seq_len), seq_len // 50):
            seq[j] = rng.choice("ACGT")
        recs.append(
            {
                "processid": f"SYN{i}",
                "split": "train",
                "dna_barcode": "".join(seq),
                "dna_bin": f"BIN{s}",
                "order": "Diptera",
                "family": f"Fam{s % 3}",
                "subfamily": "",
                "genus": f"Genus{s}",
                "species": f"Genus{s} species{s}",
                "image_path": "",
            }
        )
    return recs
