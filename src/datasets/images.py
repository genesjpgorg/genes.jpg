"""Concurrent, polite, validating image downloader.

Typical use::

    tasks = [ImageTask("gbif:123", url, root / "images" / "9606" / "gbif_123", meta={...})]
    results = download_images(tasks)
    records = [to_image_record(r, root) for r in results if r.ok]
    write_failures(results, root / "logs" / "image_failures.jsonl")

Every image is sniffed and decoded with PIL before it is accepted (a
``Content-Type: image/jpeg`` header is not trusted), streamed to a unique
``<stem>.<random>.part`` and atomically renamed to ``<stem>.<ext>`` with the extension
derived from the detected format.

``dest`` identifies the image, not ``url``: an intact file already at ``<stem>.<ext>`` is
re-validated and reused without a request (no provenance check), and tasks in one batch
that share a stem fail after the first one (``duplicate dest``) so a result never describes
bytes written by another task.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from random import random
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import requests
from PIL import Image, UnidentifiedImageError
from tqdm import tqdm

from .schema import ImageRecord

USER_AGENT = "genes.jpg/0.1 (+https://github.com/genesjpgorg/genes.jpg)"

IMAGE_EXTENSIONS: dict[str, str] = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "TIFF": "tiff",
}
"""PIL format name -> file extension. Multi-picture JPEGs (PIL format ``MPO``, common from
phone cameras) are reported as ``JPEG``; images in any other format are rejected. Animated
GIFs and multi-page TIFFs are validated and sized on their first frame."""

MAX_PIXELS = 50_000_000
"""Default ``max_pixels``: images declaring more pixels are rejected before any decode. A
2 MB PNG can expand to hundreds of MB, so this bounds per-worker memory."""

_KNOWN_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"}
_CHUNK = 1 << 16
_RETRY_AFTER_MAX = 60.0
_MAX_REDIRECTS = 5


@dataclass
class ImageTask:
    image_id: str
    url: str
    dest: Path
    """Target path; a known image suffix is replaced by the detected one, so it may be
    given without extension (``images/9606/gbif_123`` -> ``images/9606/gbif_123.jpg``)."""
    meta: dict = field(default_factory=dict)
    """Free-form metadata carried through to the result; keys that are ``ImageRecord``
    fields are used by :func:`to_image_record`."""

    def __post_init__(self) -> None:
        self.dest = Path(self.dest)


@dataclass
class ImageResult:
    task: ImageTask
    ok: bool
    path: Path | None = None
    sha256: str | None = None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    mode: str | None = None
    error: str | None = None
    downloaded_at: datetime | None = None
    reused: bool = False
    """True when an existing valid file was re-validated instead of downloaded."""


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ImageRejected(Exception):
    """Permanent failure for this URL (4xx, not an image, too small, ...)."""


class _HostRateLimiter:
    """Token bucket per host. ``acquire`` reserves a token and sleeps for the deficit,
    so concurrent callers are spaced out at ``rps`` requests per second."""

    def __init__(self, rps: float, burst: float | None = None):
        self._rps = rps
        self._capacity = burst if burst is not None else max(1.0, rps)
        self._state: dict[str, tuple[float, float]] = {}  # host -> (tokens, updated_at)
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        if self._rps <= 0:
            return
        with self._lock:
            now = time.monotonic()
            tokens, updated = self._state.get(host, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - updated) * self._rps) - 1.0
            self._state[host] = (tokens, now)
        if tokens < 0:
            time.sleep(-tokens / self._rps)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), _RETRY_AFTER_MAX)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max(0.0, (when - datetime.now(UTC)).total_seconds()), _RETRY_AFTER_MAX)


def _split_dest(dest: Path) -> Path:
    """Path without its (known) image suffix; the detected extension is appended later."""
    return dest.with_suffix("") if dest.suffix.lower() in _KNOWN_SUFFIXES else dest


def _candidate_paths(dest: Path) -> list[Path]:
    stem = _split_dest(dest)
    candidates = [stem.with_name(f"{stem.name}.{ext}") for ext in IMAGE_EXTENSIONS.values()]
    if dest != stem:
        candidates.insert(0, dest)
    return list(dict.fromkeys(candidates))


def _read_image(path: Path, max_pixels: int | None) -> tuple[int, int, str, str]:
    """Open, verify and decode ``path``; ``ImageRejected`` for anything PIL cannot read."""
    try:
        with Image.open(path) as im:
            width, height, fmt, mode = im.width, im.height, im.format or "", im.mode
            if max_pixels and width * height > max_pixels:
                raise ImageRejected(f"too large: {width}x{height} > max_pixels={max_pixels}")
            im.verify()
        with Image.open(path) as im:
            # verify() does not catch truncated JPEGs: decode, at 1/8 scale where the codec
            # supports it (JPEG DCT scaling; draft() is a no-op for the other formats).
            im.draft(None, (max(1, width // 8), max(1, height // 8)))
            im.load()
    except ImageRejected:
        raise
    except UnidentifiedImageError as e:
        raise ImageRejected("not an image") from e
    except Image.DecompressionBombError as e:
        raise ImageRejected(f"too large: {e}") from e
    except (FileNotFoundError, PermissionError):
        raise
    except Exception as e:  # PIL plugins raise assorted exception types on malformed input
        raise ImageRejected(f"corrupt image: {type(e).__name__}: {e}") from e
    return width, height, "JPEG" if fmt == "MPO" else fmt, mode


def _policy_error(width: int, height: int, fmt: str, min_side: int) -> str | None:
    if fmt not in IMAGE_EXTENSIONS:
        return f"unsupported image format {fmt!r}"
    if min(width, height) < min_side:
        return f"too small: {width}x{height} < min_side={min_side}"
    return None


def inspect_image(
    path: Path, *, min_side: int = 0, max_pixels: int | None = MAX_PIXELS
) -> tuple[int, int, str, str]:
    """Validate an image file and return ``(width, height, format, mode)``.

    Raises ``ImageRejected`` when PIL cannot identify or decode the file (truncated data,
    decompression bomb), it declares more than ``max_pixels`` pixels, its format is not in
    :data:`IMAGE_EXTENSIONS` or ``min(width, height) < min_side``.
    """
    width, height, fmt, mode = _read_image(path, max_pixels)
    if (error := _policy_error(width, height, fmt, min_side)) is not None:
        raise ImageRejected(error)
    return width, height, fmt, mode


def _sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


class _Downloader:
    def __init__(
        self,
        *,
        per_host_rps: float,
        timeout: float,
        total_timeout: float | None,
        retries: int,
        min_side: int,
        max_pixels: int | None,
        user_agent: str,
        backoff: float,
        max_bytes: int | None,
    ):
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.retries = retries
        self.min_side = min_side
        self.max_pixels = max_pixels
        self.backoff = backoff
        self.max_bytes = max_bytes
        self.headers = {"User-Agent": user_agent, "Accept": "image/*,*/*;q=0.8"}
        self.limiter = _HostRateLimiter(per_host_rps)
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            session.headers.update(self.headers)
        return session

    def run(self, task: ImageTask) -> ImageResult:
        stem = _split_dest(task.dest)
        # unique temp name: another process writing the same stem can never share our file
        part = stem.with_name(f"{stem.name}.{uuid4().hex[:12]}.part")
        try:
            existing = self._reuse_existing(task)
            if existing is not None:
                return existing
            stem.parent.mkdir(parents=True, exist_ok=True)
            sha256, n_bytes = self._fetch_with_retries(task.url, part)
            width, height, fmt, mode = inspect_image(
                part, min_side=self.min_side, max_pixels=self.max_pixels
            )
            final = stem.with_name(f"{stem.name}.{IMAGE_EXTENSIONS[fmt]}")
            os.replace(part, final)
        except Exception as e:  # noqa: BLE001 - contract: one bad image never raises
            return ImageResult(task, ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            part.unlink(missing_ok=True)
        return ImageResult(
            task,
            ok=True,
            path=final,
            sha256=sha256,
            bytes=n_bytes,
            width=width,
            height=height,
            format=fmt,
            mode=mode,
            downloaded_at=datetime.now(UTC),
        )

    def _reuse_existing(self, task: ImageTask) -> ImageResult | None:
        for path in _candidate_paths(task.dest):
            if not path.is_file():
                continue
            try:
                width, height, fmt, mode = _read_image(path, self.max_pixels)
            except ImageRejected:
                path.unlink(missing_ok=True)  # unreadable leftover: download again
                continue
            if (error := _policy_error(width, height, fmt, self.min_side)) is not None:
                # intact but outside the current policy: keep the file (fetching the same
                # URL again would only reproduce it) and fail the task
                return ImageResult(
                    task, ok=False, error=f"ImageRejected: {error} (existing {path.name} kept)"
                )
            sha256, n_bytes = _sha256_of(path)
            return ImageResult(
                task,
                ok=True,
                path=path,
                sha256=sha256,
                bytes=n_bytes,
                width=width,
                height=height,
                format=fmt,
                mode=mode,
                downloaded_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                reused=True,
            )
        return None

    def _fetch_with_retries(self, url: str, part: Path) -> tuple[str, int]:
        for attempt in range(self.retries + 1):
            try:
                return self._fetch(url, part)
            except (
                _Retryable,
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as e:
                if attempt >= self.retries:
                    raise
                delay = self.backoff * 2**attempt * (1 + 0.25 * random())
                if isinstance(e, _Retryable) and e.retry_after is not None:
                    delay = max(delay, e.retry_after)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _fetch(self, url: str, part: Path) -> tuple[str, int]:
        """One attempt: follow redirects (rate-limiting every host touched), stream the body
        to ``part`` and return ``(sha256, bytes)``. Bounded by ``total_timeout``."""
        expired = threading.Event()
        response: requests.Response | None = None

        def abort() -> None:
            # requests' timeout only bounds single socket reads, so a trickling host would
            # hold the worker indefinitely; shutdown() wakes the blocked read (urllib3 >= 2.3).
            expired.set()
            shutdown = getattr(response.raw, "shutdown", None) if response is not None else None
            if shutdown is not None:
                try:
                    shutdown()
                except (OSError, ValueError):
                    pass

        timer = threading.Timer(self.total_timeout, abort) if self.total_timeout else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                self.limiter.acquire(urlsplit(url).hostname or "")
                response = self.session.get(
                    url, stream=True, timeout=self.timeout, allow_redirects=False
                )
                with response as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers["Location"])
                        continue
                    return self._save_body(r, part, expired)
            raise ImageRejected(f"more than {_MAX_REDIRECTS} redirects")
        except Exception as e:
            if expired.is_set():
                raise _Retryable(f"transfer exceeded total_timeout={self.total_timeout}s") from e
            raise
        finally:
            if timer is not None:
                timer.cancel()

    def _save_body(self, r: requests.Response, part: Path, expired: threading.Event):
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(
                f"HTTP {r.status_code}", _parse_retry_after(r.headers.get("Retry-After"))
            )
        if r.status_code != 200:
            raise ImageRejected(f"HTTP {r.status_code}")
        length = r.headers.get("Content-Length")
        if self.max_bytes and length and length.isdigit() and int(length) > self.max_bytes:
            raise ImageRejected(f"Content-Length {length} exceeds max_bytes={self.max_bytes}")
        h = hashlib.sha256()
        n = 0
        with part.open("wb") as f:
            for chunk in r.iter_content(_CHUNK):
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
                if self.max_bytes and n > self.max_bytes:
                    raise ImageRejected(f"body exceeds max_bytes={self.max_bytes}")
                if expired.is_set():
                    break
        if expired.is_set():
            raise _Retryable(f"transfer exceeded total_timeout={self.total_timeout}s")
        if n == 0:
            raise ImageRejected("empty body")
        return h.hexdigest(), n


def download_images(
    tasks: Sequence[ImageTask],
    *,
    max_workers: int = 16,
    per_host_rps: float = 4.0,
    timeout: float = 30,
    total_timeout: float | None = 300,
    retries: int = 3,
    min_side: int = 64,
    max_pixels: int | None = MAX_PIXELS,
    user_agent: str = USER_AGENT,
    backoff: float = 1.0,
    max_bytes: int | None = 64 << 20,
    progress: bool = True,
) -> list[ImageResult]:
    """Download and validate ``tasks`` concurrently; one ``ImageResult`` per task, in order.

    Never raises for a single image: failures come back as ``ok=False`` with ``error`` set
    and no file (not even a ``.part``) left behind. ``retries`` applies to connection
    errors, timeouts, 429 and 5xx responses (exponential ``backoff`` honoring
    ``Retry-After``) and to attempts exceeding ``total_timeout`` seconds (``timeout`` only
    bounds single socket reads). Other HTTP errors, non-image bodies, corrupt/truncated
    files, unsupported formats, images over ``max_pixels``/``max_bytes`` and images with
    ``min(width, height) < min_side`` fail immediately. Redirects are followed (up to 5),
    rate-limiting every host touched.

    A task whose ``dest`` shares a stem with an earlier task in the batch fails with
    ``duplicate dest``. An intact file already at the dest is reused without a request; if
    it fails the current ``min_side``/format policy the task fails and the file is kept.
    Ctrl-C cancels queued tasks; running downloads finish (or fail) and clean up.
    """
    downloader = _Downloader(
        per_host_rps=per_host_rps,
        timeout=timeout,
        total_timeout=total_timeout,
        retries=retries,
        min_side=min_side,
        max_pixels=max_pixels,
        user_agent=user_agent,
        backoff=backoff,
        max_bytes=max_bytes,
    )
    results: list[ImageResult | None] = [None] * len(tasks)
    first_by_stem: dict[str, int] = {}
    pending: list[int] = []
    for i, task in enumerate(tasks):
        stem = _split_dest(task.dest)
        first = first_by_stem.setdefault(os.path.abspath(stem), i)
        if first == i:
            pending.append(i)
        else:
            error = f"duplicate dest {stem} (also task {tasks[first].image_id!r})"
            results[i] = ImageResult(task, ok=False, error=error)
    if not pending:
        return [r for r in results if r is not None]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pending)))) as pool:
        futures = {pool.submit(downloader.run, tasks[i]): i for i in pending}
        try:
            for future in tqdm(
                as_completed(futures), total=len(futures), unit="img", disable=not progress
            ):
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as e:  # noqa: BLE001 - contract: one bad image never raises
                    results[i] = ImageResult(tasks[i], ok=False, error=f"{type(e).__name__}: {e}")
        except BaseException:  # Ctrl-C: drop queued work instead of draining the whole batch
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    return [r for r in results if r is not None]


def to_image_record(result: ImageResult, root: Path, **fields: object) -> ImageRecord:
    """Build an ``ImageRecord`` from a successful result.

    ``image_id``, ``source_url`` and the file fields come from the result; the remaining
    fields come from ``task.meta`` (keys that are ``ImageRecord`` fields) overridden by
    ``**fields``. ``file`` is POSIX and relative to ``root``.
    """
    if not result.ok or result.path is None:
        raise ValueError(f"cannot build record for failed image {result.task.image_id}")
    names = set(ImageRecord.model_fields) | {
        f.alias for f in ImageRecord.model_fields.values() if f.alias
    }
    data: dict[str, object] = {k: v for k, v in result.task.meta.items() if k in names}
    data.update(fields)
    data.update(
        image_id=result.task.image_id,
        source_url=result.task.url,
        file=result.path.resolve().relative_to(Path(root).resolve()).as_posix(),
        sha256=result.sha256,
        bytes=result.bytes,
        width=result.width,
        height=result.height,
        format=result.format,
        downloaded_at=result.downloaded_at or datetime.now(UTC),
    )
    return ImageRecord(**data)  # type: ignore[arg-type]


def write_failures(results: Iterable[ImageResult], path: Path, *, append: bool = False) -> Path:
    """Write failed results as JSON lines (image_id, url, dest, meta, error).

    Overwrites ``path`` unless ``append=True`` (use it when logging several batches, e.g.
    one ``download_images`` call per species, to one ``logs/image_failures.jsonl``).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w") as f:
        for r in results:
            if r.ok:
                continue
            row = asdict(r.task)
            row["dest"] = Path(row["dest"]).as_posix()
            row["error"] = r.error
            f.write(json.dumps(row, default=str) + "\n")
    return path
