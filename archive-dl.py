#!/usr/bin/env python3
"""
archive-dl — download media from any Internet Archive item.

Takes any archive.org URL (details / download / metadata), a bare identifier,
and pulls the media out of it. No third-party packages; Python 3.8+ stdlib only.

  python archive-dl.py https://archive.org/details/capture-a-3065
  python archive-dl.py capture-a-3065 --list
  python archive-dl.py https://archive.org/details/Gung_Ho --format mp4
  python archive-dl.py --search 'collection:feature_films AND subject:"film noir"' --limit 10
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

METADATA_URL = "https://archive.org/metadata/{id}"
SEARCH_URL = "https://archive.org/advancedsearch.php"
DOWNLOAD_URL = "https://archive.org/download/{id}/{file}"
UA = "archive-dl/1.0 (+https://github.com/)"

# Extensions we consider media, and the order we prefer them in.
VIDEO_EXT = ("mp4", "mkv", "webm", "ogv", "avi", "m4v", "mpg", "mpeg", "mov", "wmv", "flv", "ts")
AUDIO_EXT = ("mp3", "ogg", "oga", "flac", "m4a", "wav", "opus", "aac")
IMAGE_EXT = ("jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp")
TEXT_EXT = ("pdf", "txt", "epub", "djvu", "xml", "json", "csv", "md")

# The Archive stores its own bookkeeping files inside every item. They are not
# content and should never appear in a listing: <id>_meta.xml, <id>_files.xml,
# <id>_meta.sqlite, <id>_archive.torrent, <id>_reviews.xml.
SIDECAR_RE = re.compile(
    r"(_meta\.(xml|sqlite)|_files\.xml|_archive\.torrent|_reviews\.xml)$", re.I
)


# ---------------------------------------------------------------- helpers

def human_size(n):
    """1234567 -> '1.2 MB'. Returns '?' for junk input."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def human_time(secs):
    """291.38 -> '4m51s'. Used for media runtimes."""
    try:
        secs = int(float(secs))
    except (TypeError, ValueError):
        return ""
    if secs < 60:
        return "%ds" % secs
    m, s = divmod(secs, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def ext_of(name):
    """Lowercase extension, ignoring any query string."""
    name = name.split("?", 1)[0]
    base, dot, tail = name.rpartition(".")
    return tail.lower() if dot else ""


def kind_of(name):
    """Classify a filename into video / audio / image / text / other."""
    e = ext_of(name)
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in IMAGE_EXT:
        return "image"
    if e in TEXT_EXT:
        return "text"
    return "other"


def extract_identifier(raw):
    """
    Pull the archive.org identifier out of whatever the user pasted.

    Accepts:
      https://archive.org/details/<id>[/...][?query]
      https://archive.org/download/<id>[/file]
      https://archive.org/metadata/<id>
      https://archive.org/embed/<id>
      <id>
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""

    # Bare identifier (no scheme, no slash)
    if "://" not in raw and "/" not in raw:
        return raw

    # Normalise a scheme-less URL so urlparse has something to chew on
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urllib.parse.urlparse(raw)
    path = parsed.path or ""

    # Known path prefixes, longest first
    for prefix in ("/details/", "/download/", "/metadata/", "/embed/", "/stream/"):
        if prefix in path:
            rest = path.split(prefix, 1)[1]
            # Take only the first segment; the rest may be a filename
            return urllib.parse.unquote(rest.split("/", 1)[0]).strip()

    # Fall back to the last non-empty path segment
    segs = [s for s in path.split("/") if s]
    return urllib.parse.unquote(segs[-1]).strip() if segs else ""


def http_json(url, timeout=30):
    """GET a URL and parse JSON. Raises RuntimeError with a readable message."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s for %s" % (e.code, url)) from e
    except urllib.error.URLError as e:
        raise RuntimeError("network error for %s: %s" % (url, e.reason)) from e
    except json.JSONDecodeError as e:
        raise RuntimeError("server did not return JSON for %s" % url) from e


def fetch_metadata(ident):
    """Return the item metadata dict, or raise RuntimeError."""
    data = http_json(METADATA_URL.format(id=urllib.parse.quote(ident)))
    if not isinstance(data, dict) or not data.get("files"):
        # The API returns {"is_dark": true} for restricted items and {} for missing ones.
        if isinstance(data, dict) and data.get("is_dark"):
            raise RuntimeError("item '%s' is restricted (dark) — no files available" % ident)
        raise RuntimeError("no files found for item '%s' (wrong identifier?)" % ident)
    return data


# ---------------------------------------------------------------- selection

def select_files(files, want_format=None, want_kind=None, include_derivatives=True):
    """
    Choose which files to download.

    An Archive item stores every upload plus the derivatives it generated from
    them. For a film that means the uploader's original (often an odd format
    like DivX or WMV) AND a transcoded h.264 mp4. We want the derivative by
    default: it plays anywhere and is what the web player uses.

    Returns a list of file dicts, ordered video -> audio -> image -> text -> other.
    """
    out = []
    for f in files:
        name = f.get("name") or ""
        if not name or name.startswith("__") or name.endswith(".afpk"):
            continue
        # Generated thumbnail strips live in a sibling folder named "<id>.thumbs/"
        if ".thumbs/" in name or "/thumbs/" in name:
            continue
        # Archive bookkeeping, not content
        if SIDECAR_RE.search(name):
            continue
        # Hidden/dotfiles
        if name.startswith("."):
            continue
        k = kind_of(name)
        if want_kind and k != want_kind:
            continue
        if want_format:
            wanted = [w.strip().lower().lstrip(".") for w in want_format.split(",") if w.strip()]
            if ext_of(name) not in wanted:
                continue
        if not include_derivatives and f.get("source") == "derivative":
            continue
        item = dict(f)
        item["_kind"] = k
        item["_ext"] = ext_of(name)
        out.append(item)

    order = {"video": 0, "audio": 1, "image": 2, "text": 3, "other": 4}
    # Within a kind, prefer h.264 mp4 derivatives, then anything else, then originals.
    def rank(f):
        pref = 0 if f.get("format", "").lower() in ("h.264", "mpeg4") else 1
        src = 0 if f.get("source") == "derivative" else 1
        return (order.get(f["_kind"], 9), pref, src, f.get("name", ""))

    return sorted(out, key=rank)


def best_of(files, kind):
    """The single best file of a given kind, or None."""
    got = [f for f in files if f.get("_kind") == kind]
    return got[0] if got else None


# ---------------------------------------------------------------- download

def file_md5(path, buf_size=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download_file(ident, f, outdir=".", quiet=False, verify=False, retries=3):
    """
    Download one file, resuming from a .part file if one exists.

    The Archive redirects /download/ URLs to a storage node. urllib follows
    redirects by default, which is the equivalent of curl's -L. The node
    honours Range requests, so resume works.
    """
    name = f["name"]
    # Keep any subdirectory the Archive uses (e.g. 'subdir/file.mp4')
    dest = os.path.join(outdir, name.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    expected = f.get("size")
    try:
        expected = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected = None

    if os.path.exists(dest) and expected and os.path.getsize(dest) == expected:
        if not quiet:
            print("  = already have %s (%s)" % (name, human_size(expected)))
        return 0

    url = DOWNLOAD_URL.format(id=urllib.parse.quote(ident), file=urllib.parse.quote(name))
    part = dest + ".part"
    attempt = 0

    while True:
        attempt += 1
        pos = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"User-Agent": UA}
        if pos:
            headers["Range"] = "bytes=%d-" % pos

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                # If we asked for a range but got 200, the server ignored it: restart.
                if pos and r.status == 200:
                    pos = 0
                    try:
                        os.remove(part)
                    except OSError:
                        pass

                total = expected
                if total is None:
                    cl = r.headers.get("Content-Length")
                    total = (int(cl) + pos) if cl and cl.isdigit() else None

                mode = "ab" if pos else "wb"
                done = pos
                t0 = time.time()
                last = 0.0
                with open(part, mode) as out:
                    while True:
                        chunk = r.read(1 << 17)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if not quiet:
                            now = time.time()
                            if now - last > 0.1 or (total and done >= total):
                                last = now
                                rate = done / max(now - t0 + 1e-9, 1e-9)
                                if total:
                                    pct = 100.0 * done / total
                                    bar = "#" * int(pct / 4) + "-" * (25 - int(pct / 4))
                                    sys.stdout.write(
                                        "\r  [%s] %5.1f%%  %s / %s  %s/s   "
                                        % (bar, pct, human_size(done), human_size(total), human_size(rate))
                                    )
                                else:
                                    sys.stdout.write("\r  %s  %s/s   " % (human_size(done), human_size(rate)))
                                sys.stdout.flush()
            if not quiet:
                sys.stdout.write("\r" + " " * 90 + "\r")
                sys.stdout.flush()

            # Move into place
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(part, dest)

            if expected and os.path.getsize(dest) != expected:
                raise RuntimeError(
                    "size mismatch: got %d bytes, expected %d" % (os.path.getsize(dest), expected)
                )

            if verify and f.get("md5"):
                got = file_md5(dest)
                if got != f["md5"]:
                    raise RuntimeError("md5 mismatch for %s\n  expected %s\n  got      %s"
                                       % (name, f["md5"], got))

            return 0

        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, OSError) as e:
            if attempt > retries:
                if not quiet:
                    sys.stdout.write("\n")
                print("  ! failed %s: %s" % (name, e), file=sys.stderr)
                return 1
            wait = 2 ** attempt
            if not quiet:
                sys.stdout.write("\n  ... retry %d/%d in %ds (%s)\n" % (attempt, retries, wait, e))
            time.sleep(wait)


# ---------------------------------------------------------------- search

def search(query, limit=25, mediatype=None):
    """
    Search archive.org. Returns a list of {identifier, title, ...} dicts.

    NOTE: the mediatype filter must go *inside* the `q` string. Passing it as
    `fq=mediatype:movies` makes the API reject the whole request with
    "[UNSUPPORTED_VALUE] ... mediatype:movies for request parameter fq" --
    in every quoting form (bare, quoted, array). That silently yields zero
    results if you don't check for an `error` key, which is exactly what
    happened in testing.
    """
    q = query
    if mediatype:
        q = "(%s) AND mediatype:%s" % (query, mediatype)

    params = [("q", q), ("rows", str(limit)), ("page", "1"), ("output", "json")]
    for fld in ("identifier", "title", "mediatype", "year", "downloads"):
        params.append(("fl[]", fld))

    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    data = http_json(url)

    # The API reports problems as {"error": "..."} with HTTP 200. Never swallow it.
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("archive.org search error: %s" % data["error"])

    try:
        return data["response"]["docs"]
    except (KeyError, TypeError):
        raise RuntimeError("unexpected search response shape: %r" % (str(data)[:200],))


# ---------------------------------------------------------------- commands

def cmd_list(ident, args):
    meta = fetch_metadata(ident)
    md = meta.get("metadata", {})
    files = select_files(meta.get("files", []), args.format, args.kind,
                         include_derivatives=not args.no_derivatives)

    print("Item:       %s" % ident)
    if md.get("title"):
        print("Title:      %s" % md["title"])
    if md.get("uploader"):
        print("Uploader:   %s" % md["uploader"])
    if md.get("licenseurl"):
        print("License:    %s" % md["licenseurl"])
    print("Files:      %d matching (%d in item)" % (len(files), len(meta.get("files", []))))
    print()

    if not files:
        print("  (nothing matched your filters)")
        return 0

    w = max(len(f["name"]) for f in files)
    w = min(w, 60)
    for f in files:
        bits = [f.get("format") or f["_ext"] or "?"]
        if f.get("size"):
            bits.append(human_size(f["size"]))
        if f.get("length"):
            bits.append(human_time(f["length"]))
        if f.get("width") and f.get("height"):
            bits.append("%sx%s" % (f["width"], f["height"]))
        if f.get("source") == "original":
            bits.append("original")
        print("  %-*s  %s" % (w, f["name"][:w], " | ".join(bits)))
    return 0


def cmd_download(ident, args):
    meta = fetch_metadata(ident)
    md = meta.get("metadata", {})
    files = select_files(meta.get("files", []), args.format, args.kind,
                         include_derivatives=not args.no_derivatives)

    if not files:
        print("No files matched the given filters.", file=sys.stderr)
        return 1

    # Default behaviour: the best single video, matching how the web player
    # behaves. --all grabs everything that matched.
    if args.all:
        chosen = files
    elif args.kind or args.format or args.no_derivatives:
        chosen = files
    else:
        first = best_of(files, "video") or files[0]
        chosen = [first]

    if md.get("title"):
        print("Item:    %s  (%s)" % (ident, md["title"]))
    else:
        print("Item:    %s" % ident)
    print("Files:   %d" % len(chosen))
    print()

    failures = 0
    for f in chosen:
        print("  %s  [%s, %s]" % (f["name"], f.get("format") or f["_ext"], human_size(f.get("size"))))
        failures += download_file(ident, f, args.out, quiet=args.quiet,
                                  verify=args.verify, retries=args.retries)
    print()
    print("Done. %d file(s) -> %s" % (len(chosen) - failures, os.path.abspath(args.out)))
    if failures:
        print("%d download(s) failed." % failures, file=sys.stderr)
    return 1 if failures else 0


def cmd_search(args):
    docs = search(args.search, limit=args.limit, mediatype=args.mediatype)
    if not docs:
        print("No results.")
        return 0
    for d in docs:
        ident = d.get("identifier", "?")
        title = d.get("title", "")
        if isinstance(title, list):
            title = title[0] if title else ""
        extra = " ".join(str(x) for x in (d.get("year", ""), d.get("mediatype", "")) if x)
        print("  %-40s  %s%s" % (ident[:40], title[:70], ("  [" + extra + "]") if extra else ""))
    print()
    print("Download any of these with:  python %s <identifier>"
          % os.path.basename(sys.argv[0]))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="archive-dl",
        description="Download media from any Internet Archive item.",
        epilog="Examples:\n"
               "  archive-dl.py https://archive.org/details/Gung_Ho\n"
               "  archive-dl.py capture-a-3065 --list\n"
               "  archive-dl.py Gung_Ho --kind video --all --verify\n"
               "  archive-dl.py --search 'collection:feature_films AND subject:\"film noir\"'\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?", help="archive.org URL (details/download/metadata) or bare identifier")
    p.add_argument("--list", action="store_true", help="list available files, download nothing")
    p.add_argument("--all", action="store_true", help="download every matching file (default: best video only)")
    p.add_argument("--kind", choices=["video", "audio", "image", "text", "other"], help="only files of this kind")
    p.add_argument("--format", help="only these extensions, comma separated (e.g. mp4,mkv)")
    p.add_argument("--no-derivatives", action="store_true", help="skip transcoded derivatives, originals only")
    p.add_argument("--out", "-o", default=".", help="output directory (default: current)")
    p.add_argument("--verify", action="store_true", help="check md5 after download (slower)")
    p.add_argument("--quiet", "-q", action="store_true", help="no progress output")
    p.add_argument("--retries", type=int, default=3, help="retry attempts per file (default 3)")
    p.add_argument("--search", help="search archive.org instead of downloading")
    p.add_argument("--mediatype", default="movies", help="mediatype filter for --search (default movies)")
    p.add_argument("--limit", type=int, default=25, help="max search results (default 25)")

    args = p.parse_args(argv)

    try:
        if args.search:
            return cmd_search(args)

        if not args.target:
            p.print_help()
            return 2

        ident = extract_identifier(args.target)
        if not ident:
            print("Could not find an archive.org identifier in: %s" % args.target, file=sys.stderr)
            return 2

        if args.list:
            return cmd_list(ident, args)
        return cmd_download(ident, args)
    except RuntimeError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
