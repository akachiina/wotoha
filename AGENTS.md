# AGENTS.md — wotoha

Single-package Python CLI (`wotoha/`), entrypoint `wotoha.cli:main` (console script `wotoha`). Requires Python >=3.10. No tests, no CI, no lint/typecheck config in repo.

**Language policy: everything is English — CLI messages, help texts, code comments, docstrings, README, and commit messages.** (The app was originally Portuguese; do not reintroduce PT strings. User-facing aliases `"ultimo"/"último"` remain accepted as `--chapters latest` synonyms for back-compat.)

## Setup

- `pip install -e .` — core deps: `wasmtime`, `beautifulsoup4`, `lxml`, `requests`, `rich`, `Pillow`.
- Optional extras: `pip install wotoha[cb7]` (needs `py7zr` for `--format cb7`); `pip install wotoha[dev]` (pytest+ruff, but no tests or ruff config exist yet).
- SOCKS proxy (`--proxy socks5://...`) needs `pip install requests[socks]` — not declared in pyproject.
- PDF output uses Pillow only (no reportlab call in code despite the `pdf` extra).

## Architecture

- Default engine is **aidoku** (WASM, no Java): `aidoku_runtime.py` (1300+-line wasmtime host implementing Aidoku next-SDK 0.7 **and 0.8** ABI) + `postcard.py` + `models.py` codecs → `aidoku_catalog.py` (search/manga/chapters/pages facade) → `native_downloader.py` → `formats.py` → `comicinfo.py`.
- SDK 0.8 compatibility notes: `net::set_timeout` returns i32; `env::abort` (Rust panic path) and `env::sleep` (capped at 10s) are registered; `defaults.set` must NOT call `free_result` on the source-passed buffer (arguments belong to the source — freeing caused memory traps).
- **wasmtime Store is NOT thread-safe.** Concurrent WASM calls on the shared runtime store (e.g. parallel `process_page_image` in download workers) corrupt the trap stack and crash the whole process (SIGSEGV/SIGABRT, "assertion failed: core::ptr::eq(head, self)" in traphandlers/stackwalk). Every WASM entry in `aidoku_runtime.py` must hold `self._wasm_lock` — new export calls included.
- **Signals and WASM never mix**: a `KeyboardInterrupt` raised inside a WASM/host frame makes wasmtime's unwinder panic too. Worker threads block SIGINT (`config.block_sigint` as the ThreadPoolExecutor initializer); the main thread defers it around each WASM call (`_sigint_deferred`) so KI is delivered at a Python bytecode boundary. Ctrl+C UX: download loop catches KI → "Interrupted — partial files kept. Re-run the same command to resume." → exit 130.
- Page downloads treat HTTP 4xx as permanent (no retry/backoff) **within a release**; any failed page fails that release and the downloader falls back to the next release of the same chapter (multi-group sources like MangaDex return one entry per group — `list_chapters` dedupes them, extras become `NativeChapter.fallbacks`). The chapter only saves when ALL pages download from some release; otherwise `MangaDlError` + no `.complete` marker, so re-running retries it.
- MangaDex CDN (`*.mangadex.network`) **flaps**: specific image files of a release return 404 for windows of minutes/hours and then work again with the exact same URL (verified: same URL 404 from the CLI and 200 from a plain request minutes apart; not headers, not concurrency). Release fallback + re-running later is the remedy — don't "fix" this with aggressive in-run retries.
- MangaPlus protobuf normalization: `_normalize_mangaplus_directory` in `aidoku_runtime.py` rewrites `title_list/all_v3` responses to keep only the 1st language variant per entry (the source's prost model merges repeated singular `title` fields and drops EN titles otherwise).
- Sources come from **named repos** (`~/.wotoha/repos.json`, built-in `aidoku`): `aidoku_store.py` has `aggregate_indexes`/`find_source` (search/install across all repos) and `record_source_repo`/`repo_of` (provenance in `~/.wotoha/source_repos.json`, shown as the Repo column in tables). CLI: `wotoha sources repo add|list|remove`, `--index <name|url>` accepted by `sources` itself and its subcommands (parent uses `default=None`, subparser parent uses `SUPPRESS`).
- Legacy engine **suwayomi** (`client.py`, `downloader.py`, `server_manager.py`, needs JVM + `wotoha server download && wotoha server start`): only for `--engine suwayomi` paths. IDs differ — aidoku uses composite `sourceId::key`, suwayomi uses numeric ID.
- `cli.py` dispatches per-engine (`_download_aidoku` vs `_download_suwayomi`); shared helpers in `net.py` (proxy/rate-limit), `batch.py` (`--pick`, batch file parsing), `config.py`.
- `--language/-l` (search/info/download) overrides `config.languages` for that run (feeds the `defaults.get("languages")` filter that MangaDex/MangaPlus consult); default stays `WOTOHA_LANGUAGES` ("eng", mapped to 639-1 via `_LANG_MAP` in the runtime).
- `REF/` contains read-only reference clones (rakuyomi, mangadex-downloader, suwayomi-server, mihon), gitignored — never edit; `formats.py` header notes which REF files it was ported from.

## Conventions / gotchas

- Config is env-only via `config.py` + minimal `.env` loader (`KEY=VALUE`, no quotes/expansion, `setdefault` so real env wins): `MANGA_DL_URL`, `MANGA_DL_FORMAT`, `MANGA_DL_DIR` (default `./downloads`), `MANGA_DL_SERVER_JAR`, `WOTOHA_SOURCES_DIR` (default `~/.wotoha/sources`), `WOTOHA_PROXY`, `WOTOHA_TIMEOUT`, `WOTOHA_RATE_CALLS`/`WOTOHA_RATE_SECONDS`, `WOTOHA_LANGUAGES`. `-o/--output` overrides `download_dir` per-run.
- Sources are `.aix` zips in `~/.wotoha/sources/<id>.aix` (`Payload/main.wasm` + `source.json`); `aidoku_store.py:fetch_index` hits `https://aidoku-community.github.io/sources/index.min.json` live — `sources list` degrades to installed-only offline.
- `formats.py:get_format(name, base_dir, mode)` monkey-patches `save_chapter` to `_save_chapter_single` when `mode == "single"`; `RawFormat` renames (not copies) the chapter workdir, other formats use `<download_dir>/.cache` + `.wotoha-staging` temp dirs. Skip-if-exists: chapter/volume/single outputs are skipped when the target file/dir already exists — delete output to force re-download.
- `cb7` raises `MangaDlError` without `py7zr`; `EpubFormat`/`PdfFormat` accumulate volume/single via `_Staging` and finalize in `finish()` — callers must always call `fmt.finish(manga_stub)`.
- Chapter selection strings (`-c`, `--pages`, `--start/end-chapter`) parse in `downloader.py:parse_page_range` / `select_chapters` — reuse, don't reinvent.
- Error handling in `cli.py:main`: catch `MangaDlError`, `requests.HTTPError`, `requests.RequestException`, `FileNotFoundError`, `KeyboardInterrupt` (exit 130); per-chapter failures are caught in `_download_aidoku` (`MangaDlError`/`ValueError`/`RequestException`) so one bad chapter doesn't kill the run; log with `logging.getLogger("wotoha")`, `-v` switches to DEBUG.

## Workflow

- Do not append `🤖 Generated with Codebuff` / `Co-Authored-By: Codebuff <noreply@codebuff.com>` footers to commit messages.
- **Write commit messages in English.**
- Default language flags: `WOTOHA_LANGUAGES` env (default `eng`) or `--language en|pt-BR|...` per command.
- Verify changes with `.venv/bin/python -m py_compile wotoha/*.py` (no test suite) and, for source-affecting changes, a real `wotoha search/info` against an installed source (mangalivre/flaky sites may need a retry).
