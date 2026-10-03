"""Tests for datasets.images against a local HTTP server (offline) plus one GBIF smoke test."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import threading
import time
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from PIL import Image

from datasets import images
from datasets.images import (
    ImageRejected,
    ImageResult,
    ImageTask,
    _Downloader,
    _HostRateLimiter,
    _parse_retry_after,
    download_images,
    inspect_image,
    to_image_record,
    write_failures,
)
from datasets.schema import ImageRecord


def make_image(fmt: str, size: tuple[int, int] = (128, 96)) -> bytes:
    im = Image.new("P" if fmt == "GIF" else "RGB", size)
    px = im.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = (x * y) % 256 if fmt == "GIF" else (x % 256, y % 256, (x * y) % 256)
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def make_mpo() -> bytes:
    """Multi-picture JPEG as written by phone cameras (PIL opens it with format 'MPO')."""
    buf = io.BytesIO()
    Image.new("RGB", (128, 96), "red").save(
        buf, "MPO", save_all=True, append_images=[Image.new("RGB", (64, 48), "blue")]
    )
    return buf.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """Syntactically valid PNG whose IHDR declares ``width x height`` with no real pixel data."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


JPEG = make_image("JPEG")
PNG = make_image("PNG")
WEBP = make_image("WEBP")
GIF = make_image("GIF")
TIFF = make_image("TIFF")
TINY = make_image("PNG", (10, 10))
MPO = make_mpo()
BOMB = png_header_only(20000, 20000)  # > PIL's DecompressionBombError threshold
BIG = png_header_only(9000, 9000)  # 81 Mpx: under PIL's limit, over MAX_PIXELS


def _send(h: BaseHTTPRequestHandler, status: int, ctype: str, body: bytes, headers=None) -> None:
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def raw(fn):
    """Route handled by ``fn(handler, hit_number)`` writing the response itself."""
    fn.raw = True
    return fn


def _partial(h: BaseHTTPRequestHandler) -> None:
    """Declare the full JPEG length, send half, then let the server drop the connection."""
    h.send_response(200)
    h.send_header("Content-Type", "image/jpeg")
    h.send_header("Content-Length", str(len(JPEG)))
    h.end_headers()
    h.wfile.write(JPEG[: len(JPEG) // 2])
    h.wfile.flush()


@raw
def _half_then_ok(h, n):
    if n <= 2:
        _partial(h)
    else:
        _send(h, 200, "image/jpeg", JPEG)


@raw
def _always_partial(h, n):
    _partial(h)


@raw
def _drip(h, n):
    h.send_response(200)
    h.send_header("Content-Type", "image/jpeg")
    h.send_header("Content-Length", "60")
    h.end_headers()
    for _ in range(60):
        try:
            h.wfile.write(b"x")
            h.wfile.flush()
        except OSError:  # client gave up
            return
        time.sleep(0.2)


@raw
def _slow_png(h, n):
    h.send_response(200)
    h.send_header("Content-Type", "image/png")
    h.send_header("Content-Length", str(len(PNG)))
    h.end_headers()
    h.wfile.write(PNG[: len(PNG) // 2])
    h.wfile.flush()
    time.sleep(0.3)
    h.wfile.write(PNG[len(PNG) // 2 :])


@raw
def _redirect(h, n):
    port = h.server.server_address[1]
    _send(h, 302, "text/plain", b"", {"Location": f"http://127.0.0.1:{port}/ok.jpg"})


@raw
def _redirect_loop(h, n):
    _send(h, 302, "text/plain", b"", {"Location": "/loop.jpg"})


# path -> (status, content_type, body[, headers]) ; callables get the hit number (1-based)
ROUTES = {
    "/ok.jpg": (200, "image/jpeg", JPEG),
    "/ok.png": (200, "image/png", PNG),
    "/noext": (200, "application/octet-stream", JPEG),
    "/lying.png": (200, "image/png", JPEG),  # says png, is jpeg
    "/ok.webp": (200, "image/webp", WEBP),
    "/ok.gif": (200, "image/gif", GIF),
    "/ok.tiff": (200, "image/tiff", TIFF),
    "/phone.jpg": (200, "image/jpeg", MPO),
    "/html.jpg": (200, "text/html", b"<html><body>Not Found</body></html>"),
    "/truncated.jpg": (200, "image/jpeg", JPEG[: len(JPEG) // 2]),
    "/tiny.png": (200, "image/png", TINY),
    "/bomb.png": (200, "image/png", BOMB),
    "/big.png": (200, "image/png", BIG),
    "/empty.jpg": (200, "image/jpeg", b""),
    "/missing.jpg": (404, "text/plain", b"nope"),
    "/always500.jpg": (500, "text/plain", b"boom"),
    "/flaky.jpg": lambda n: (500, "text/plain", b"boom") if n <= 2 else (200, "image/jpeg", JPEG),
    "/ratelimited.jpg": lambda n: (
        (429, "text/plain", b"slow down", {"Retry-After": "1"})
        if n == 1
        else (200, "image/jpeg", JPEG)
    ),
    "/halfclose.jpg": _half_then_ok,
    "/partial.jpg": _always_partial,
    "/drip.jpg": _drip,
    "/slow.png": _slow_png,
    "/redir.jpg": _redirect,
    "/loop.jpg": _redirect_loop,
}


class _Server:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.times: dict[str, list[float]] = defaultdict(list)
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                with server.lock:
                    server.hits[self.path] += 1
                    n = server.hits[self.path]
                    server.times[self.path].append(time.monotonic())
                route = ROUTES.get(self.path)
                if route is None:
                    self.send_error(404)
                elif getattr(route, "raw", False):
                    route(self, n)
                else:
                    _send(self, *(route(n) if callable(route) else route))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return self.base + path

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def server():
    s = _Server()
    yield s
    s.close()


def _dl(tasks, **kw):
    kw.setdefault("progress", False)
    kw.setdefault("backoff", 0.01)
    kw.setdefault("per_host_rps", 0)  # unlimited; rate limiting has its own tests
    return download_images(tasks, **kw)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_download_valid_images_and_extension_inference(server, tmp_path: Path):
    tasks = [
        ImageTask("a", server.url("/ok.jpg"), tmp_path / "x" / "a"),
        ImageTask("b", server.url("/ok.png"), tmp_path / "x" / "b.png"),
        ImageTask("c", server.url("/noext"), tmp_path / "c"),
        ImageTask("d", server.url("/lying.png"), tmp_path / "d.png"),
        ImageTask("e", server.url("/ok.webp"), tmp_path / "e"),
        ImageTask("f", server.url("/ok.gif"), tmp_path / "f"),
        ImageTask("g", server.url("/ok.tiff"), tmp_path / "g"),
    ]
    results = _dl(tasks)
    assert [r.task.image_id for r in results] == list("abcdefg")
    assert all(r.ok for r in results), [r.error for r in results]
    assert [r.path.name for r in results] == [
        "a.jpg",
        "b.png",
        "c.jpg",
        "d.jpg",  # extension follows the sniffed format, not the given suffix
        "e.webp",
        "f.gif",
        "g.tiff",
    ]
    assert [r.format for r in results] == ["JPEG", "PNG", "JPEG", "JPEG", "WEBP", "GIF", "TIFF"]
    r = results[0]
    assert (r.width, r.height) == (128, 96)
    assert r.bytes == len(JPEG) == r.path.stat().st_size
    assert (
        r.sha256
        == hashlib.sha256(JPEG).hexdigest()
        == hashlib.sha256(r.path.read_bytes()).hexdigest()
    )
    assert r.downloaded_at is not None and r.downloaded_at.tzinfo is not None
    assert not r.reused
    assert not (tmp_path / "d.png").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_mpo_jpeg_is_accepted_as_jpeg(server, tmp_path: Path):
    (r,) = _dl([ImageTask("p", server.url("/phone.jpg"), tmp_path / "phone")])
    assert r.ok, r.error
    assert r.path.name == "phone.jpg" and r.format == "JPEG"
    assert (r.width, r.height) == (128, 96)  # primary frame


def test_str_dest_is_accepted(server, tmp_path: Path):
    task = ImageTask("s", server.url("/ok.jpg"), str(tmp_path / "s"))
    assert isinstance(task.dest, Path)
    (r,) = _dl([task])
    assert r.ok and r.path == tmp_path / "s.jpg"


def test_rejections_leave_no_files(server, tmp_path: Path):
    tasks = [
        ImageTask("html", server.url("/html.jpg"), tmp_path / "html"),
        ImageTask("trunc", server.url("/truncated.jpg"), tmp_path / "trunc"),
        ImageTask("tiny", server.url("/tiny.png"), tmp_path / "tiny"),
        ImageTask("empty", server.url("/empty.jpg"), tmp_path / "empty"),
        ImageTask("missing", server.url("/missing.jpg"), tmp_path / "missing"),
        ImageTask("conn", "http://127.0.0.1:9/x.jpg", tmp_path / "conn"),  # refused port
        ImageTask("bomb", server.url("/bomb.png"), tmp_path / "bomb"),
        ImageTask("big", server.url("/big.png"), tmp_path / "big"),
        ImageTask("partial", server.url("/partial.jpg"), tmp_path / "partial"),
        ImageTask("loop", server.url("/loop.jpg"), tmp_path / "loop"),
    ]
    results = _dl(tasks, retries=1)
    assert not any(r.ok for r in results)
    errors = {r.task.image_id: r.error for r in results}
    assert "not an image" in errors["html"]
    assert "truncated" in errors["trunc"]
    assert "too small" in errors["tiny"] and "10x10" in errors["tiny"]
    assert "empty body" in errors["empty"]
    assert "HTTP 404" in errors["missing"]
    assert "ConnectionError" in errors["conn"]
    assert "too large" in errors["bomb"]
    assert "max_pixels" in errors["big"]
    assert "redirects" in errors["loop"]
    assert server.hits["/missing.jpg"] == 1  # 4xx is not retried
    assert server.hits["/truncated.jpg"] == 1
    assert server.hits["/partial.jpg"] == 2  # declared length not met: retried, then given up
    assert server.hits["/loop.jpg"] == 6  # 1 + 5 redirects, no retry
    assert not _files(tmp_path)
    for r in results:
        assert r.path is None and r.sha256 is None


def test_unreadable_existing_file_is_replaced(server, tmp_path: Path):
    for name, payload in [("a.jpg", b"garbage"), ("b.png", BOMB), ("c.png", BIG)]:
        (tmp_path / name).write_bytes(payload)
    tasks = [ImageTask(n, server.url("/ok.jpg"), tmp_path / n) for n in "abc"]
    results = _dl(tasks)
    for r in results:
        assert r.ok and not r.reused, r.error
        assert r.path == tmp_path / f"{r.task.image_id}.jpg"
        assert r.path.read_bytes() == JPEG
    assert _files(tmp_path) == [tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"]


def test_min_side_is_configurable(server, tmp_path: Path):
    (r,) = _dl([ImageTask("tiny", server.url("/tiny.png"), tmp_path / "tiny")], min_side=10)
    assert r.ok and (r.width, r.height) == (10, 10)


def test_stricter_policy_on_rerun_keeps_existing_file(server, tmp_path: Path):
    task = ImageTask("t", server.url("/tiny.png"), tmp_path / "t")
    (first,) = _dl([task], min_side=8)
    assert first.ok
    hits = server.hits["/tiny.png"]
    (second,) = _dl([task], min_side=64)
    assert not second.ok and "too small" in second.error and "kept" in second.error
    assert first.path.exists() and first.path.read_bytes() == TINY
    assert server.hits["/tiny.png"] == hits  # same URL would only reproduce the file


def test_retry_on_5xx_then_success(server, tmp_path: Path):
    (r,) = _dl([ImageTask("flaky", server.url("/flaky.jpg"), tmp_path / "flaky")], retries=3)
    assert r.ok, r.error
    assert server.hits["/flaky.jpg"] == 3
    assert r.path.name == "flaky.jpg"


def test_retry_on_mid_body_disconnect(server, tmp_path: Path):
    (r,) = _dl([ImageTask("h", server.url("/halfclose.jpg"), tmp_path / "h")], retries=3)
    assert r.ok, r.error
    assert server.hits["/halfclose.jpg"] == 3
    assert r.path.read_bytes() == JPEG and not list(tmp_path.glob("*.part"))


def test_retries_exhausted(server, tmp_path: Path):
    (r,) = _dl([ImageTask("x", server.url("/always500.jpg"), tmp_path / "x")], retries=2)
    assert not r.ok and "HTTP 500" in r.error
    assert server.hits["/always500.jpg"] == 3  # 1 + retries
    assert not list(tmp_path.iterdir())


def test_429_honours_retry_after(server, tmp_path: Path):
    t0 = time.monotonic()
    (r,) = _dl([ImageTask("rl", server.url("/ratelimited.jpg"), tmp_path / "rl")], retries=1)
    elapsed = time.monotonic() - t0
    assert r.ok, r.error
    assert server.hits["/ratelimited.jpg"] == 2
    assert elapsed >= 1.0


def test_total_timeout_bounds_slow_transfer(server, tmp_path: Path):
    t0 = time.monotonic()
    (r,) = _dl(
        [ImageTask("d", server.url("/drip.jpg"), tmp_path / "d")],
        timeout=0.5,
        total_timeout=1.0,
        retries=0,
    )
    elapsed = time.monotonic() - t0
    assert not r.ok and "total_timeout" in r.error
    assert elapsed < 3, elapsed  # the drip would take 12 s otherwise
    assert not list(tmp_path.iterdir())


def test_idempotent_rerun_does_not_refetch(server, tmp_path: Path):
    tasks = [ImageTask("a", server.url("/ok.jpg"), tmp_path / "a")]
    (first,) = _dl(tasks)
    hits = server.hits["/ok.jpg"]
    # second run: same dest without extension -> finds a.jpg, re-validates, no GET
    (second,) = _dl(tasks)
    assert server.hits["/ok.jpg"] == hits
    assert second.ok and second.reused and not first.reused
    assert second.path == first.path and second.sha256 == first.sha256
    assert (second.width, second.height, second.format) == (128, 96, "JPEG")
    assert second.downloaded_at is not None
    # dest given with a different (wrong) suffix still finds the existing file
    (third,) = _dl([ImageTask("a", server.url("/ok.jpg"), tmp_path / "a.png")])
    assert third.reused and third.path == first.path
    assert server.hits["/ok.jpg"] == hits


def test_duplicate_dests_in_one_batch(server, tmp_path: Path):
    # different URLs, same stem ("img" and "img.png"): only the first is fetched
    tasks = [
        ImageTask("A", server.url("/ok.jpg"), tmp_path / "img"),
        ImageTask("B", server.url("/slow.png"), tmp_path / "img.png"),
    ]
    a, b = _dl(tasks, max_workers=2)
    assert a.ok and a.path == tmp_path / "img.jpg"
    assert a.sha256 == hashlib.sha256(a.path.read_bytes()).hexdigest()
    assert not b.ok and "duplicate dest" in b.error and "'A'" in b.error
    assert server.hits["/slow.png"] == 0
    # identical rows (and the very same task object twice): one download, one file
    same = [ImageTask(f"s{i}", server.url("/ok.jpg"), tmp_path / "same") for i in range(4)]
    hits = server.hits["/ok.jpg"]
    results = _dl(same + [same[0]], max_workers=8)
    assert [r.ok for r in results] == [True, False, False, False, False]
    assert all("duplicate dest" in r.error for r in results[1:])
    assert server.hits["/ok.jpg"] == hits + 1
    assert _files(tmp_path) == [tmp_path / "img.jpg", tmp_path / "same.jpg"]
    assert results[0].sha256 == hashlib.sha256((tmp_path / "same.jpg").read_bytes()).hexdigest()


def test_concurrency_and_result_order(server, tmp_path: Path):
    tasks = [ImageTask(f"i{i}", server.url("/ok.jpg"), tmp_path / f"i{i}") for i in range(40)]
    results = _dl(tasks, max_workers=8)
    assert [r.task.image_id for r in results] == [t.image_id for t in tasks]
    assert all(r.ok for r in results)
    assert len({r.path for r in results}) == 40


def test_keyboard_interrupt_cancels_queued_tasks(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def fake_run(self, task):
        calls.append(task.image_id)
        if task.image_id == "i0":
            raise KeyboardInterrupt
        time.sleep(0.05)
        return ImageResult(task, ok=True)

    monkeypatch.setattr(images._Downloader, "run", fake_run)
    tasks = [ImageTask(f"i{i}", "http://example.invalid/x", tmp_path / f"i{i}") for i in range(100)]
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        download_images(tasks, max_workers=2, progress=False)
    assert time.monotonic() - t0 < 2
    assert len(calls) < 10  # queued work was cancelled, not drained


def test_host_rate_limiter_spaces_requests():
    limiter = _HostRateLimiter(rps=50, burst=1)
    t0 = time.monotonic()
    threads = [threading.Thread(target=limiter.acquire, args=("h",)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - t0 >= 5 / 50 * 0.9  # 1 free token, 5 spaced at 20 ms
    # another host has its own bucket
    t0 = time.monotonic()
    limiter.acquire("other")
    assert time.monotonic() - t0 < 0.01


def test_rate_limit_applies_through_the_pool(server, tmp_path: Path):
    with server.lock:
        server.times["/ok.jpg"].clear()
    tasks = [ImageTask(f"r{i}", server.url("/ok.jpg"), tmp_path / f"r{i}") for i in range(15)]
    results = _dl(tasks, per_host_rps=10, max_workers=16)
    assert all(r.ok for r in results)
    times = server.times["/ok.jpg"]
    assert len(times) == 15
    assert max(times) - min(times) >= 0.4  # burst of 10, then 5 spaced at 100 ms


def test_redirects_rate_limit_every_host(server, tmp_path: Path):
    d = _Downloader(
        per_host_rps=0,
        timeout=5,
        total_timeout=None,
        retries=0,
        min_side=0,
        max_pixels=None,
        user_agent="test",
        backoff=0,
        max_bytes=None,
    )
    acquired: list[str] = []
    d.limiter.acquire = acquired.append
    r = d.run(ImageTask("r", f"http://localhost:{server.port}/redir.jpg", tmp_path / "r"))
    assert r.ok, r.error
    assert r.path == tmp_path / "r.jpg" and r.path.read_bytes() == JPEG
    assert acquired == ["localhost", "127.0.0.1"]  # origin and redirect target
    (r2,) = _dl([ImageTask("r2", server.url("/redir.jpg"), tmp_path / "r2")])
    assert r2.ok and r2.path.name == "r2.jpg"


def test_parse_retry_after():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("3") == 3.0
    assert _parse_retry_after("9999") == 60.0  # capped
    http_date = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    assert 3.0 < _parse_retry_after(http_date) <= 5.0
    assert _parse_retry_after("garbage") is None


def test_inspect_image_direct(tmp_path: Path):
    p = tmp_path / "x"
    p.write_bytes(PNG)
    assert inspect_image(p) == (128, 96, "PNG", "RGB")
    p.write_bytes(MPO)
    assert inspect_image(p) == (128, 96, "JPEG", "RGB")
    p.write_bytes(b"not an image at all")
    with pytest.raises(ImageRejected, match="not an image"):
        inspect_image(p)
    bmp = io.BytesIO()
    Image.new("RGB", (70, 70)).save(bmp, "BMP")
    p.write_bytes(bmp.getvalue())
    with pytest.raises(ImageRejected, match="unsupported"):
        inspect_image(p)
    p.write_bytes(BOMB)
    with pytest.raises(ImageRejected, match="too large"):
        inspect_image(p)
    p.write_bytes(BIG)
    with pytest.raises(ImageRejected, match="max_pixels"):
        inspect_image(p)
    with pytest.raises(ImageRejected, match="corrupt"):  # cap lifted: decoded, no pixel data
        inspect_image(p, max_pixels=None)
    with pytest.raises(FileNotFoundError):  # I/O errors are not disguised as bad images
        inspect_image(tmp_path / "nope")


def test_to_image_record_and_failures(server, tmp_path: Path):
    root = tmp_path / "ds"
    meta = {
        "ncbi_taxid": 9689,
        "source_dataset": "gbif",
        "source_provider": "inaturalist",
        "source_id": "123",
        "original_label": "Panthera leo",
        "license": "CC BY-NC 4.0",
        "gbif_key": 5938104699,  # not an ImageRecord field: ignored
    }
    tasks = [
        ImageTask("gbif:123", server.url("/ok.jpg"), root / "images" / "9689" / "gbif_123", meta),
        ImageTask("gbif:124", server.url("/html.jpg"), root / "images" / "9689" / "gbif_124", meta),
    ]
    ok, bad = _dl(tasks)
    rec = to_image_record(ok, root, publisher="iNaturalist")
    assert isinstance(rec, ImageRecord)
    assert rec.file == "images/9689/gbif_123.jpg"
    assert rec.image_id == "gbif:123" and rec.ncbi_taxid == 9689
    assert rec.source_url == ok.task.url and rec.publisher == "iNaturalist"
    assert rec.license == "CC BY-NC 4.0" and rec.original_label == "Panthera leo"
    assert (rec.width, rec.height, rec.format, rec.bytes) == (128, 96, "JPEG", len(JPEG))
    assert rec.sha256 == ok.sha256 and rec.downloaded_at == ok.downloaded_at
    assert rec.image_type == "unknown"
    with pytest.raises(ValueError):
        to_image_record(bad, root)

    log = write_failures([ok, bad], root / "logs" / "image_failures.jsonl")
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["image_id"] == "gbif:124"
    assert lines[0]["url"] == bad.task.url
    assert "not an image" in lines[0]["error"]
    assert lines[0]["meta"]["ncbi_taxid"] == 9689
    assert lines[0]["dest"].endswith("images/9689/gbif_124")
    # overwrite by default, append on request
    write_failures([bad], log)
    assert len(log.read_text().splitlines()) == 1
    write_failures([bad], log, append=True)
    assert len(log.read_text().splitlines()) == 2


def test_empty_task_list():
    assert download_images([], progress=False) == []


def test_result_dataclass_defaults():
    task = ImageTask("x", "http://example.invalid/x", Path("x"))
    r = ImageResult(task, ok=False, error="boom")
    assert r.path is None and r.bytes is None and r.format is None and not r.reused
    assert task.meta == {}


@pytest.mark.network
def test_gbif_real_images(tmp_path: Path):
    resp = requests.get(
        "https://api.gbif.org/v1/occurrence/search",
        params={"taxonKey": 5219404, "mediaType": "StillImage", "limit": 3},
        headers={"User-Agent": "genes.jpg/0.1 (+https://github.com/genesjpgorg/genes.jpg)"},
        timeout=30,
    )
    resp.raise_for_status()
    tasks = []
    for occ in resp.json()["results"]:
        for m in occ.get("media", []):
            if m.get("type") == "StillImage" and m.get("identifier"):
                tasks.append(
                    ImageTask(
                        f"gbif:{occ['key']}:{len(tasks)}",
                        m["identifier"],
                        tmp_path
                        / "images"
                        / str(occ.get("speciesKey", 0))
                        / f"gbif_{occ['key']}_{len(tasks)}",
                        meta={
                            "ncbi_taxid": 9689,
                            "source_dataset": "gbif",
                            "original_label": occ.get("species", occ.get("scientificName", "")),
                            "license": m.get("license"),
                            "rights_holder": m.get("rightsHolder"),
                            "publisher": m.get("publisher"),
                        },
                    )
                )
            if len(tasks) >= 2:
                break
        if len(tasks) >= 2:
            break
    assert len(tasks) == 2
    results = download_images(tasks, max_workers=2, per_host_rps=2.0, progress=False)
    assert all(r.ok for r in results), [r.error for r in results]
    for r in results:
        assert r.path.exists() and r.path.stat().st_size == r.bytes
        assert r.format in {"JPEG", "PNG", "WEBP"} and min(r.width, r.height) >= 64
        rec = to_image_record(r, tmp_path)
        assert rec.file.startswith("images/")
