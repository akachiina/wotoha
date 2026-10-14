<h1 align="center">wotoha</h1>

<p align="center">
  Manga downloader CLI powered by <a href="https://github.com/aidoku-community">Aidoku</a> (WebAssembly) extensions — lightweight, pure Python, no Java
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/aidoku-SDK_0.7_+_0.8-purple.svg" alt="Aidoku"></a>
  <a href="#"><img src="https://img.shields.io/badge/engine-wasmtime-orange.svg" alt="wasmtime"></a>
</p>

```
┌──────────┐  runs     ┌────────────────────┐  scrapes  ┌──────────────┐
│  wotoha  │ ────────► │  .aix extension    │ ────────► │  manga       │
│ (Python) │  wasmtime │  (WASM sandboxed)  │           │  sites       │
└──────────┘           └────────────────────┘           └──────────────┘
```

---

## Features

- **140+ sources** — official Aidoku index plus third-party repos (`sources repo add`)
- **~80 MB RAM** — no JVM, no Android, no Chromium, just Python + wasmtime
- **5 output formats** — `raw`, `epub`, `cbz`, `cb7`, `pdf`, all with ComicInfo.xml metadata
- **3 packaging modes** — per `chapter`, per `volume`, or `single` file for the whole manga
- **Language filter** — per-run `--language` for multi-language sources (MangaDex, MangaPlus, MangaFire...)
- **Resume built in** — re-run the same command, finished chapters are skipped
- **Offline convert** — repackage downloaded folders without re-downloading
- **Optional Suwayomi engine** — Mihon extensions via `--engine suwayomi` (needs a JVM)

> Inspired by [rakuyomi](https://github.com/tachibana-shin/rakuyomi) (Aidoku runtime for KOReader)
> and [mangadex-downloader](https://github.com/mansuf/mangadex-downloader) (CLI UX).

## Installation

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

Optional extras: `pip install wotoha[cb7]` (needs `py7zr` for `--format cb7`);
SOCKS proxy (`--proxy socks5://...`) needs `pip install requests[socks]`.

## Quick Start

```bash
# 1. Install a source
wotoha sources search asura
wotoha sources install en.asurascans

# 2. Find a manga (IDs are composite: sourceId::key)
wotoha search "solo leveling" --source en.asurascans

# 3. Preview, then download (files land in the current directory)
wotoha download en.asurascans::solo-leveling -c 1-10 --dry-run
wotoha download en.asurascans::solo-leveling -f epub -c 1-10
```

## Usage

### Sources

```bash
wotoha sources list                        # installed first, then available
wotoha sources list --filter asura         # match by ID or name
wotoha sources list --lang pt-BR           # filter by language
wotoha sources list --repo yomu            # filter by repository
wotoha sources list --installed-only --limit 10
wotoha sources search asura --limit 5
wotoha sources install en.asurascans
wotoha sources uninstall en.asurascans

# Third-party repositories (any Aidoku-format index.min.json)
wotoha sources repo add yomu https://smexhy.github.io/yomu-aidoku-sources/index.min.json
wotoha sources repo list
wotoha sources install --index yomu en.atsumaru
wotoha sources repo remove yomu
```

`install` looks the source up in every registered repo automatically;
`--index <name|url>` forces a specific one.

### Search & info

```bash
wotoha search "berserk"                              # every installed source
wotoha search "berserk" --source en.asurascans
wotoha search "solo" --pick                          # pick rows, download latest
wotoha search "solo" --pick -c 1-10 -f epub          # pick, download a range
wotoha search "solo" --json                          # machine-readable output

wotoha info en.asurascans::solo-leveling
wotoha info multi.mangadex::<uuid> -l pt-BR --show-chapters 0
```

`--language/-l` sets the **chapter** language on multi-language sources
(default: `WOTOHA_LANGUAGES`, i.e. `eng`). It does not filter search rows.

### Download

```bash
wotoha download en.asurascans::solo-leveling -f epub              # everything
wotoha download en.asurascans::solo-leveling -f cbz -c latest     # latest only
wotoha download en.asurascans::solo-leveling -f pdf -c 1-10,15    # ranges
wotoha download multi.mangadex::<uuid> -l pt-BR -c 1-10
wotoha download ID --dry-run                          # preview, download nothing
wotoha download ID1 ID2 ID3 -f epub                   # batch: multiple IDs
wotoha download --batch-from-file list.txt -f cbz     # batch: one ID per line
wotoha download ... -o ./my-folder                    # custom output directory
```

Chapter selection:

| Flag | Meaning |
|---|---|
| `-c all` (default) | every chapter |
| `-c latest` | most recent chapter only |
| `-c 1-10,15,20-23` | ranges by chapter number |
| `--start-chapter 10 --end-chapter 20` | numeric cut, inclusive |
| `--pages 1-10` | pages per chapter (`5-` = from 5 on, `3--` = last 3, `-5` = drop last 5) |
| `--no-oneshot` | skip oneshot chapters |
| `--order asc` | oldest first (default: newest first) |

`--mode chapter` (default) packs one file per chapter, `volume` one per
volume, `single` the whole manga into one file. `--cover` saves the manga
cover; `--use-volume-cover` inserts it as the first page of each output.

Network: `--workers 4` (parallel pages), `--retries 3`,
`--delay-requests 2.5` (rate limit), `--proxy socks5://localhost:9050`.

A summary table (`ok` / `failed` / `skipped`) ends every run — one bad
chapter never kills the batch. Exit codes: `0` ok, `1` error,
`130` interrupted (partial files kept, re-run to resume).

### Convert

Repackage downloaded folders without touching the network:

```bash
wotoha convert ./My-Manga -f cbz                  # each chapter folder -> .cbz
wotoha convert ./My-Manga -f epub --mode single   # whole manga -> one .epub
wotoha convert ./A ./B -f pdf -o ./out
```

Each input is a manga folder (subfolders with images) or a single chapter
folder (images directly). `--title` overrides the manga title.

### Formats

| Format | Output |
|---|---|
| `raw` | `Title/Chapter 1/0001.webp ...` |
| `epub` | one `.epub` per chapter |
| `cbz` | one `.cbz` per chapter |
| `cb7` | one `.cb7` per chapter (needs `wotoha[cb7]`) |
| `pdf` | one `.pdf` per chapter |

Existing outputs are skipped, never overwritten — delete the file to
force a re-download.

## Configuration

Environment only (a minimal `.env` in the working directory is also read:
`KEY=VALUE`, no quotes/expansion, real env wins):

| Variable | Default | Meaning |
|---|---|---|
| `MANGA_DL_DIR` | current directory | output dir (`-o` overrides per-run) |
| `MANGA_DL_FORMAT` | `raw` | default `--format` |
| `WOTOHA_SOURCES_DIR` | `~/.wotoha/sources` | installed `.aix` sources |
| `WOTOHA_LANGUAGES` | `eng` | default `--language` |
| `WOTOHA_PROXY` | — | default `--proxy` |
| `WOTOHA_TIMEOUT` | `30` | HTTP timeout, seconds |
| `WOTOHA_RATE_CALLS` / `WOTOHA_RATE_SECONDS` | `0` / `2.5` | rate limit (`--delay-requests N` = 1 req / N sec) |
| `MANGA_DL_URL` | `http://localhost:4567` | Suwayomi server URL (legacy engine) |

## Project Structure

```
wotoha/
├── cli.py                # commands: sources/search/info/download/convert
├── aidoku_runtime.py     # WASM host: Aidoku next-SDK 0.7 and 0.8 ABI
├── aidoku_store.py       # index + .aix install, named repos
├── aidoku_catalog.py     # search/manga/chapters/pages facade
├── native_downloader.py  # chapter downloads (resume, retries, parallel)
├── postcard.py           # postcard codec (ABI serialization)
├── models.py             # Manga/Chapter/Page binary codecs
├── formats.py            # raw/epub/cbz/cb7/pdf + chapter/volume/single
├── comicinfo.py          # ComicInfo.xml metadata
├── net.py                # proxy, timeout, rate limit
├── batch.py              # --pick selection, batch file parsing
├── config.py             # env config + .env loader
├── client.py             # (legacy suwayomi engine)
├── downloader.py         # (legacy suwayomi engine)
└── server_manager.py     # (legacy suwayomi engine)
```

The runtime implements the same ABI rakuyomi and the Aidoku app use: the
WASM source calls host imports (`net.send`, `html.select`,
`std.read_buffer`...) and returns postcard-serialized structs that wotoha
decodes. Fully sandboxed — no disk/network access except through host
functions.

## Suwayomi (optional, legacy engine)

Mihon/keiyoushi extensions, needs a JVM:

```bash
wotoha server download && wotoha server start
wotoha extensions install eu.kanade.tachiyomi.extension.en.weebcentral
wotoha search "one piece" --engine suwayomi --source 2131019126180322627
wotoha download <id> --engine suwayomi -f epub
```

Same commands under both engines (suwayomi IDs are numeric;
`--batch-from-file` is aidoku-only).

## Requirements

- Python 3.10+
- `wasmtime`, `beautifulsoup4`, `lxml`, `requests`, `rich`, `Pillow`

## Legal

Respect the sites' terms of service and the works' copyright. Only download
content you have the right to access.

## License

MIT
