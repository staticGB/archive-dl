# archive-dl

Paste any Internet Archive item link → get the direct video file links.

Two things in here, use whichever you want:

| | what it is | needs |
|---|---|---|
| **`index.html`** | Web converter. Paste a link, get direct video URLs with copy buttons. | nothing — one file, opens in a browser |
| **`archive-dl.py`** | Command-line downloader. Same conversion, but actually downloads. | Python 3.8+, no packages |

## The web converter

Open `index.html` (or the GitHub Pages URL) and paste a link:

```
https://archive.org/details/Gung_Ho
```

You get back every video file in that item, with its size, runtime and resolution, plus:

- **Copy link** — the direct `archive.org/download/...` URL, straight to your clipboard
- **Open in new tab** — plays or saves in the browser
- **Save file…** — downloads with a live progress bar (Chrome/Edge)
- **Preview** — inline player, no download

### Deploying to GitHub Pages

1. Put `index.html` in a repo
2. Settings → Pages → Source: *Deploy from a branch* → `main` / `/ (root)`
3. Live at `https://<user>.github.io/<repo>/`

No build step, no dependencies, no API key.

## The command line

```bash
# see what's in an item without downloading
python archive-dl.py https://archive.org/details/capture-a-3065 --list

# download the best video (the transcoded mp4, not the uploader's original)
python archive-dl.py capture-a-3065 -o ~/Videos

# everything, verified
python archive-dl.py Gung_Ho --all --verify

# only certain kinds
python archive-dl.py Some_Item --kind audio --all
python archive-dl.py Some_Item --format mp4,mkv

# search, then download whatever it finds
python archive-dl.py --search 'collection:feature_films AND subject:"film noir"' --limit 10
```

Accepts `/details/`, `/download/`, `/metadata/`, `/embed/`, a scheme-less URL, or a bare identifier.

## How it works

Two public endpoints, no auth:

```
https://archive.org/metadata/<identifier>              # item inventory as JSON
https://archive.org/download/<identifier>/<filename>   # the file itself
```

The identifier is the segment after `/details/`. The metadata endpoint lists every
file in the item, and the download URL is just those two pieces joined. That's the
whole mechanism — this project is a convenience wrapper around it.

Both endpoints send `Access-Control-Allow-Origin: *`, and the download endpoint
honours `Range` requests, which is why the browser version can stream a real
progress bar instead of navigating away.

## Things worth knowing

**Take the derivative, not the original.** Every item holds the uploader's original
file *plus* transcodes the Archive generated. In practice that means choosing
between `Gung_Ho.mp4` (h.264, 513 MB) and `Gung_Ho.AVI` (DivX, 630 MB). The mp4 is
marked `source: derivative` and plays everywhere; the original may be a codec
nothing supports. Both tools sort derivatives first and mark originals.

**Bookkeeping files are filtered out.** Items contain `<id>_meta.xml`,
`<id>_files.xml`, `<id>_meta.sqlite`, `<id>_archive.torrent` and a `<id>.thumbs/`
folder of JPEGs. None of it is content.

**The mediatype search filter must go inside `q`.** `fq=mediatype:movies` is
rejected by the search API in every quoting form, and the API returns HTTP 200
with an `error` key rather than a failure status — so a naive client gets zero
results and no explanation. The CLI builds `(...) AND mediatype:movies` into the
query string instead, and raises on any `error` key.

**Resuming works but needs the `.part` file.** Downloads go to `<name>.part` and
are renamed on completion. Delete the `.part` and it starts over; leave it and the
next run sends `Range: bytes=<offset>-` and continues.

## Licence / scope

This tool has no opinion about what you point it at — it reads the public API and
returns whatever is there. Internet Archive hosts public-domain works *and*
user uploads whose rights sit with someone else; the metadata endpoint reports a
`licenseurl` when the item declares one. Check before you redistribute anything.
