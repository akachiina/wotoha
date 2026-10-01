# wotoha 🪼

Manga downloader CLI **with a local web UI** and **Aidoku (WebAssembly) extensions** — lightweight, pure Python, **no Java**.

Named after [Wotoha (をとは)](https://x.com/mitukineu), the illustrator/singer behind OFUTONE —
the UI is as cozy as her "jelly girl" world: futon pastels, soft clouds and a little jellyfish.

```
┌──────────┐  runs     ┌────────────────────┐  scrapes  ┌──────────────┐
│  wotoha  │ ────────► │  .aix extension    │ ────────► │  manga       │
│ (Python) │  wasmtime │  (WASM sandboxed)  │           │  sites       │
└──────────┘           └────────────────────┘           └──────────────┘
```

- **~80 MB RAM** total (the Suwayomi/JVM path uses ~2.6 GB)
- No JVM, no Android, no CEF/Chromium — just Python + wasmtime
- **143+ sources**: official Aidoku index + third-party repos (Yomu, etc.)
- Download as `raw`, `epub`, `cbz`, `cb7` or `pdf`
- Per-command **language filter** (`--language`)
- **Optional** Suwayomi-Server support (Mihon/keiyoushi extensions) via `--engine suwayomi`

> Inspired by [rakuyomi](https://github.com/tachibana-shin/rakuyomi) (KOReader reader that runs
> Aidoku/LNReader/Keiyoushi sources with embedded runtimes) and
> [mangadex-downloader](https://github.com/mansuf/mangadex-downloader) (CLI UX).
> The full reference lives in the `REF/` folder.

## Install

```bash
pip install -e .
```

Dependencies: `wasmtime`, `beautifulsoup4`, `requests`, `rich`, `Pillow`.

## Usage

```bash
# Sources (Aidoku extensions)
wotoha sources list                    # installed + available (aggregates all repos)
wotoha sources search asura            # search across all indexes
wotoha sources install en.asurascans   # looks the source up in every repo
wotoha sources uninstall en.asurascans

# Third-party repositories
wotoha sources repo list               # registered repos (aidoku is built in)
wotoha sources repo add yomu https://smexhy.github.io/yomu-aidoku-sources/index.min.json
wotoha sources install --index yomu en.atsumaru    # or just 'install en.atsumaru'
wotoha sources install --index https://.../index.min.json multi.kagane  # direct URL

# Discovery
wotoha search "solo leveling" --source en.asurascans
wotoha search "berserk"                # searches every installed source
wotoha info en.asurascans::solo-leveling   # link, tags, languages, latest chapter date

# Language (multi-language sources: MangaDex, MangaPlus, Mangafire...)
wotoha search "onimai" -l pt-BR
wotoha info multi.mangadex::<uuid> -l pt-BR        # translated chapters
wotoha download multi.mangadex::<uuid> -l pt-BR -c 1-10

# Download
wotoha download en.asurascans::solo-leveling -f epub              # everything
wotoha download en.asurascans::solo-leveling -f cbz -c latest     # latest chapter
wotoha download en.asurascans::solo-leveling -f pdf -c 1-10,15    # ranges
wotoha download ... -o ./my-folder

# Fine-grained selection
wotoha download ID --pages 1-10        # only pages 1-10 ("3--" = last 3; "-5" = minus last 5)
wotoha download ID --start-chapter 10 --end-chapter 20
wotoha download ID --mode volume -f epub   # pack per volume
wotoha download ID --mode single -f cbz    # whole manga in a single file
wotoha download ID --no-oneshot --order asc --cover
wotoha search "solo" --source en.asurascans --pick  # pick interactively and download

# Batch
wotoha download ID1 ID2 ID3 -f epub
wotoha download --batch-from-file list.txt -f cbz

# Network
wotoha download ID --proxy socks5://localhost:9050
wotoha download ID --delay-requests 2.5   # rate limit (1 request every 2.5s)

# Web UI (search, read online, download queue)
wotoha serve                 # http://127.0.0.1:8321
wotoha serve --port 8080 --open
```

## Web UI

`wotoha serve` starts a local web app (stdlib HTTP server, no extra deps):

- **Search** across all installed sources, with covers and language filter
- **Manga details**: cover, synopsis, tags, chapter list per language
- **Reader**: read chapters in the browser (pages proxied/cached by the host,
  hotlink protection handled per source)
- **Downloads**: queue with live progress, format/mode picker (CBZ/EPUB/PDF/RAW,
  per chapter/volume/single), cancel support — saves to the same `downloads/`
  folder as the CLI
- Light/dark themes (cozy pastel day, sleepy indigo night)

Manga IDs are composite (`sourceId::key`) and come from search results.

## Source repositories

Beyond the official Aidoku community index, wotoha accepts any repo publishing an
`index.min.json` in the same format (e.g.
[yomu-aidoku-sources](https://github.com/Smexhy/yomu-aidoku-sources)).

```bash
wotoha sources repo add <name> <index.min.json-url>
wotoha sources repo list                 # aidoku (built-in) + whatever you add
wotoha sources repo remove <name>
```

- `list`/`search` aggregate **all** repos and show the **Repo** column
  (`aidoku+yomu` when a source exists in more than one)
- `install` looks the source up in every repo; `--index <name|url>` forces one
- Provenance lives in `~/.wotoha/repos.json` (repos) and
  `~/.wotoha/source_repos.json` (origin of each installed source)
- SDK 0.8 sources are supported (Atsumaru, WEBTOON, Mangaball, Kagane...)

## Formats

| Format | Output |
|---|---|
| `raw` | `Title/Ch/001.webp ...` |
| `epub` | one `.epub` per chapter |
| `cbz` | one `.cbz` per chapter |
| `cb7` | one `.cb7` (7-zip) per chapter — requires `pip install wotoha[cb7]` |
| `pdf` | one `.pdf` per chapter |

All packed formats include **ComicInfo.xml** (title, number, volume, author,
tags, date) so Kavita/Komga/Tachiyomi pick up the metadata — in
`volume`/`single` mode with consolidated metadata.

## Architecture

```
wotoha/
├── aidoku_runtime.py   # WASM host: Aidoku next-SDK ABI (net/html/std/defaults)
├── aidoku_store.py     # index + .aix installation, named repos
├── aidoku_catalog.py   # search/manga/chapters/pages facade
├── native_downloader.py# chapter downloads (resume, retries, parallel)
├── postcard.py         # postcard codec (ABI serialization format)
├── models.py           # Manga/Chapter/Page with binary codecs
├── formats.py          # raw/epub/cbz/cb7/pdf + chapter/volume/single modes
├── comicinfo.py        # ComicInfo.xml (ComicRack metadata)
├── net.py              # shared proxy, rate limit, timeout
├── batch.py            # interactive selection (--pick) and batch parsing
├── downloader.py       # (legacy suwayomi)
├── client.py           # (legacy suwayomi)
└── server_manager.py   # (legacy suwayomi)
```

The runtime implements the same ABI rakuyomi (Rust/wasmi) and the Aidoku app
use: the WASM source calls host imports (`net.send`, `html.select`,
`std.read_buffer`...) and returns postcard-serialized structs that wotoha
decodes. Fully sandboxed: the extension has no disk/network access except
through host functions.

## Suwayomi (optional, Mihon/keiyoushi extensions)

If you also want the 1380 keiyoushi sources (they need a JVM):

```bash
wotoha server download && wotoha server start
wotoha extensions install eu.kanade.tachiyomi.extension.en.weebcentral
wotoha search "one piece" --engine suwayomi --source 2131019126180322627
wotoha download <id> --engine suwayomi -f epub
```

## Roadmap

- [x] Aidoku WASM runtime (next-SDK 0.7 **and 0.8**)
- [x] Full CLI (sources/search/info/download/convert)
- [x] ComicInfo.xml, cb7, chapter/volume/single modes, page ranges
- [x] Proxy, rate limit, batch, interactive --pick, --cover, --no-oneshot
- [x] Third-party repos (`sources repo`, `--index`, install auto-discovery)
- [x] Language filter (`--language` on search/info/download) and rich info
- [ ] JS runtime — LNReader/Mangayomi via quickjs
- [ ] Advanced search filters (`filters.json`)
- [ ] Cross-source result comparison (same title on several sources)

## Legal notes

Respect the sites' terms of service and the works' copyright. Only download
content you have the right to access.
