# wotoha

Manga downloader CLI powered by **Aidoku (WebAssembly) extensions** — lightweight, pure Python, **no Java**.

```
┌──────────┐  runs     ┌────────────────────┐  scrapes  ┌──────────────┐
│  wotoha  │ ────────► │  .aix extension    │ ────────► │  manga       │
│ (Python) │  wasmtime │  (WASM sandboxed)  │           │  sites       │
└──────────┘           └────────────────────┘           └──────────────┘
```

- **~80 MB RAM** total (the Suwayomi/JVM path uses ~2.6 GB)
- No JVM, no Android, no CEF/Chromium — just Python + wasmtime
- **140+ sources**: official Aidoku index + third-party repos
- Download as `raw`, `epub`, `cbz`, `cb7` or `pdf`, per chapter, per volume, or as a single file
- Per-run **language filter** (`--language`) for multi-language sources
- **Optional** Suwayomi-Server support (Mihon extensions) via `--engine suwayomi`

> Inspired by [rakuyomi](https://github.com/tachibana-shin/rakuyomi) (KOReader reader that runs
> Aidoku/LNReader/Keiyoushi sources with embedded runtimes) and
> [mangadex-downloader](https://github.com/mansuf/mangadex-downloader) (CLI UX).

## Install

```bash
pipx install git+https://github.com/akachiina/wotoha.git
```

Or with plain pip:

```bash
pip install git+https://github.com/akachiina/wotoha.git
```

Local development:

```bash
git clone https://github.com/akachiina/wotoha.git
cd wotoha
pip install -e .
```

Dependencies: `wasmtime`, `beautifulsoup4`, `lxml`, `requests`, `rich`, `Pillow`.
Optional: `pip install wotoha[cb7]` (needs `py7zr` for `--format cb7`);
SOCKS proxy (`--proxy socks5://...`) needs `pip install requests[socks]`.

## Quickstart

```bash
# 1. Install a source
wotoha sources search asura
wotoha sources install en.asurascans

# 2. Find a manga (IDs are composite: sourceId::key)
wotoha search "solo leveling" --source en.asurascans

# 3. Preview and download (files land in the current directory by default)
wotoha download en.asurascans::solo-leveling -c 1-10 --dry-run
wotoha download en.asurascans::solo-leveling -f epub -c 1-10
```

## Sources

```bash
wotoha sources list                        # installed first, then available
wotoha sources list --filter asura         # match by ID or name
wotoha sources list --lang pt-BR           # filter by language
wotoha sources list --repo yomu            # filter by repository
wotoha sources list --installed-only --limit 10
wotoha sources search asura --limit 5
wotoha sources install en.asurascans
wotoha sources uninstall en.asurascans
```

Third-party repositories (any repo publishing an `index.min.json`
in the Aidoku format, e.g.
[yomu-aidoku-sources](https://github.com/Smexhy/yomu-aidoku-sources)):

```bash
wotoha sources repo add yomu https://smexhy.github.io/yomu-aidoku-sources/index.min.json
wotoha sources repo list
wotoha sources install --index yomu en.atsumaru   # or just: install en.atsumaru
wotoha sources repo remove yomu
```

`install` finds the source in every registered repo automatically;
`--index <name|url>` forces a specific one. Provenance is stored in
`~/.wotoha/repos.json` and `~/.wotoha/source_repos.json` (shown as the
Repo column).

## Search & info

```bash
wotoha search "berserk"                              # all installed sources
wotoha search "berserk" --source en.asurascans
wotoha search "solo" --pick                          # pick results, download latest
wotoha search "solo" --pick -c 1-10 -f epub          # pick, download a range
wotoha search "solo" --json                          # machine-readable output

wotoha info en.asurascans::solo-leveling
wotoha info multi.mangadex::<uuid> -l pt-BR --show-chapters 0
```

`--language/-l` sets the chapter language on multi-language sources
(MangaDex, MangaPlus, MangaFire...); default comes from `WOTOHA_LANGUAGES`
(`eng`). Note: it filters **chapters**, not search-result rows.

## Download

```bash
wotoha download en.asurascans::solo-leveling -f epub              # everything
wotoha download en.asurascans::solo-leveling -f cbz -c latest     # latest chapter
wotoha download en.asurascans::solo-leveling -f pdf -c 1-10,15    # ranges
wotoha download multi.mangadex::<uuid> -l pt-BR -c 1-10
wotoha download ID --dry-run                          # preview selection only
wotoha download ID1 ID2 ID3 -f epub                   # batch: multiple IDs
wotoha download --batch-from-file list.txt -f cbz     # batch: one ID per line
wotoha download ... -o ./my-folder                    # custom output directory
```

Chapter selection:

| Flag | Meaning |
|---|---|
| `-c all` (default) | every chapter |
| `-c latest` | only the most recent chapter |
| `-c 1-10,15,20-23` | ranges by chapter number |
| `--start-chapter 10 --end-chapter 20` | numeric cut (inclusive) |
| `--pages 1-10` | only pages 1–10 of each chapter (`5-` = from 5 on, `3--` = last 3, `-5` = drop last 5) |
| `--no-oneshot` | skip oneshot chapters |
| `--order asc` | oldest first (default: newest first) |

Packaging (`--mode`): `chapter` (default, one file per chapter),
`volume` (one file per volume), `single` (whole manga in one file).
`--cover` saves the manga cover; `--use-volume-cover` inserts it as the
first page of each output.

Network: `--workers 4` (parallel pages per chapter), `--retries 3`,
`--delay-requests 2.5` (rate limit), `--proxy socks5://localhost:9050`.

A summary table (`ok` / `failed` / `skipped`) is printed at the end.
Failed chapters don't stop the run — re-run the same command to resume.
Exit codes: `0` ok, `1` error, `130` interrupted (Ctrl+C keeps partial files).

## Convert

Repackage already-downloaded folders without re-downloading
(offline, engine-independent):

```bash
wotoha convert ./My-Manga -f cbz                  # each chapter folder -> .cbz
wotoha convert ./My-Manga -f epub --mode single   # whole manga -> one .epub
wotoha convert ./A ./B -f pdf -o ./out
```

Each input is a manga folder (subfolders with images) or a single
chapter folder (images directly). `--title` overrides the manga title.

## Formats

| Format | Output |
|---|---|
| `raw` | `Title/Chapter 1/0001.webp ...` |
| `epub` | one `.epub` per chapter |
| `cbz` | one `.cbz` per chapter |
| `cb7` | one `.cb7` (7-zip) per chapter — requires `pip install wotoha[cb7]` |
| `pdf` | one `.pdf` per chapter |

All packed formats include **ComicInfo.xml** (title, number, volume, author,
tags, date) so Kavita/Komga/Tachiyomi pick up the metadata — consolidated
in `volume`/`single` mode. Existing outputs are skipped, never overwritten
(delete the file to force a re-download).

## Configuration

Everything via environment (a minimal `.env` in the working directory
is also read: `KEY=VALUE`, no quotes/expansion):

| Variable | Default | Meaning |
|---|---|---|
| `MANGA_DL_DIR` | current directory | output directory (`-o` overrides per-run) |
| `MANGA_DL_FORMAT` | `raw` | default `--format` |
| `WOTOHA_SOURCES_DIR` | `~/.wotoha/sources` | installed `.aix` sources |
| `WOTOHA_LANGUAGES` | `eng` | default `--language` |
| `WOTOHA_PROXY` | — | default `--proxy` |
| `WOTOHA_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `WOTOHA_RATE_CALLS` / `WOTOHA_RATE_SECONDS` | `0` / `2.5` | rate limit (`--delay-requests N` = 1 req per N sec) |
| `MANGA_DL_URL` | `http://localhost:4567` | Suwayomi server URL (legacy engine) |

## Suwayomi (optional, legacy engine)

For the Mihon/keiyoushi extensions (needs a JVM):

```bash
wotoha server download && wotoha server start
wotoha extensions install eu.kanade.tachiyomi.extension.en.weebcentral
wotoha search "one piece" --engine suwayomi --source 2131019126180322627
wotoha download <id> --engine suwayomi -f epub
```

Search/info/download/convert behave the same under both engines
(suwayomi IDs are numeric; `--batch-from-file` is aidoku-only).

## Legal notes

Respect the sites' terms of service and the works' copyright. Only download
content you have the right to access.
