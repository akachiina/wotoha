"""wotoha CLI.

Engines:
- aidoku (default): native WASM runtime, no Java
- suwayomi: legacy backend via Suwayomi-Server (optional)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .config import config
from .downloader import safe_name
from .errors import MangaDlError, MangaNotFound
from .formats import RawFormat, _volume_folder_name, get_format

console = Console()
err_console = Console(stderr=True)

log = logging.getLogger("wotoha")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def print_manga_table(
    mangas: list[dict], title: str, id_key: str = "id", numbered: bool = False
) -> None:
    table = Table(title=title, show_lines=False)
    if numbered:
        table.add_column("#", style="dim", no_wrap=True)
    table.add_column("ID", style="cyan", overflow="fold")
    table.add_column("Title", style="white", overflow="fold")
    if mangas and mangas[0].get("sourceId"):
        table.add_column("Source", style="green")
    if any(m.get("repo") for m in mangas):
        table.add_column("Repo", style="yellow")
    table.add_column("Status", style="magenta")
    for i, m in enumerate(mangas, start=1):
        row = []
        if numbered:
            row.append(str(i))
        row += [str(m.get(id_key, "?")), m.get("title") or "?"]
        if mangas and mangas[0].get("sourceId"):
            row.append(str(m.get("sourceId")))
        if any(m.get("repo") for m in mangas):
            row.append(m.get("repo") or "?")
        row.append((m.get("status") or "?").title())
        table.add_row(*row)
    console.print(table)


def _print_json(data) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False))


def _chapter_label(chapter) -> str:
    num = getattr(chapter, "chapter_number", None)
    title = getattr(chapter, "title", None)
    name = getattr(chapter, "name", None) or "?"
    if num is not None:
        label = f"ch. {num:g}"
        if title and title != name:
            label += f" - {title}"
        return label
    return name


def _print_download_summary(results: list[tuple[str, str, str]]) -> None:
    """Prints the per-chapter ok/failed/skipped summary (both engines)."""
    if not results:
        return
    table = Table(title="Summary")
    table.add_column("Chapter", style="white", overflow="fold")
    table.add_column("Status", style="magenta")
    table.add_column("Output", style="cyan", overflow="fold")
    for name, status, output in results:
        style = "green" if status == "ok" else ("yellow" if status == "skipped" else "red")
        table.add_row(name, f"[{style}]{status}[/{style}]", output or "-")
    console.print(table)


def _aidoku_catalog():
    from .aidoku_catalog import AidokuCatalog

    return AidokuCatalog()


def _suwayomi_client():
    from .client import SuwayomiClient

    client = SuwayomiClient()
    if not client.is_up():
        err_console.print(
            f"[red]Suwayomi is offline at {config.server_url}.[/red] "
            "Start it with: [bold]wotoha server start[/bold]"
        )
        sys.exit(1)
    return client


# --------------------------------------------------------------------------- #
# server / extensions (legacy suwayomi)
# --------------------------------------------------------------------------- #

def cmd_server(args) -> None:
    from .server_manager import download_jar, start, status, stop

    if args.action == "download":
        download_jar(force=args.force)
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "status":
        status()


def cmd_extensions(args) -> None:
    from .client import SuwayomiClient

    client = SuwayomiClient()
    if args.ext_action == "list":
        exts = client.list_extensions()
        table = Table(title=f"Suwayomi extensions ({len(exts)})")
        table.add_column("pkgName", style="cyan", overflow="fold")
        table.add_column("Name", style="white", overflow="fold")
        table.add_column("Lang", style="green")
        table.add_column("Installed", style="magenta")
        for e in exts:
            table.add_row(
                e.get("pkgName") or "?",
                e.get("name") or "?",
                e.get("lang") or "?",
                "yes" if e.get("installed", e.get("isInstalled")) else "no",
            )
        console.print(table)
    elif args.ext_action == "install":
        for pkg in args.pkg:
            print(f"Installing {pkg}...")
            client.install_extension(pkg)
            print(f"  OK: {pkg}")
    elif args.ext_action == "uninstall":
        for pkg in args.pkg:
            print(f"Uninstalling {pkg}...")
            client.uninstall_extension(pkg)
            print(f"  OK: {pkg}")


# --------------------------------------------------------------------------- #
# sources (native aidoku + suwayomi)
# --------------------------------------------------------------------------- #

def cmd_sources(args) -> None:
    if args.engine == "suwayomi":
        from .client import SuwayomiClient

        sources = SuwayomiClient().list_sources()
        table = Table(title=f"Suwayomi sources ({len(sources)})")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="white")
        table.add_column("Lang", style="green")
        for s in sources:
            table.add_row(s["id"], s.get("displayName") or s.get("name") or "?", s.get("lang") or "?")
        console.print(table)
        return

    from .aidoku_store import (
        DEFAULT_INDEX_URL,
        add_repo,
        aggregate_indexes,
        fetch_index_safe,
        find_source,
        list_installed,
        list_repos,
        remove_repo,
        resolve_index,
        sources_dir,
    )

    action = args.source_action or "list"
    index_url = DEFAULT_INDEX_URL
    try:
        if action != "repo" and getattr(args, "index", None):
            index_url = resolve_index(args.index)
    except KeyError as e:
        err_console.print(f"[red]{e.args[0]}[/red]")
        sys.exit(1)
    if action == "list":
        installed_entries = list_installed()
        installed = {s["id"] for s in installed_entries}
        if getattr(args, "index", None):
            # with an explicit --index: show that index only
            entries = fetch_index_safe(index_url)[0] or []
            repo_name = args.index
            available: dict[str, tuple[str, dict]] = {s["id"]: (repo_name, s) for s in entries}
            unreachable: list[str] = []
        else:
            available, unreachable = aggregate_indexes()
            for r in unreachable:
                err_console.print(f"[yellow]Warning: repo '{r}' is unreachable.[/yellow]")
        if getattr(args, "installed_only", False):
            all_ids = sorted(installed)
        else:
            all_ids = sorted(set(installed) | set(available))
        # installed sources first, then alphabetical
        all_ids = sorted(all_ids, key=lambda sid: (sid not in installed, sid))

        def _entry_of(sid: str) -> dict:
            if sid in available:
                return available[sid][0][1]
            entry = next((e for e in installed_entries if e["id"] == sid), None)
            return entry or {}

        def _name_of(sid: str) -> str:
            return _entry_of(sid).get("name") or "?"

        def _langs_of(sid: str) -> list[str]:
            return _entry_of(sid).get("languages") or []

        def _repo_of(sid: str) -> str:
            repos_of = available.get(sid)
            return "+".join(r for r, _ in repos_of) if repos_of else "?"

        f_text = (getattr(args, "filter", None) or "").strip().lower()
        f_lang = (getattr(args, "lang", None) or "").strip().lower()
        f_repo = (getattr(args, "repo", None) or "").strip().lower()
        filtering = bool(f_text or f_lang or f_repo)
        total_before = len(all_ids)
        if f_text:
            all_ids = [sid for sid in all_ids if f_text in sid.lower() or f_text in _name_of(sid).lower()]
        if f_lang:
            all_ids = [sid for sid in all_ids if any(f_lang == (lang or "").lower() for lang in _langs_of(sid))]
        if f_repo:
            all_ids = [sid for sid in all_ids if f_repo in _repo_of(sid).lower()]
        if not all_ids and filtering:
            console.print("[yellow]No sources match the filters.[/yellow]")
            console.print("[dim]Hint: drop --filter/--lang/--repo or try 'wotoha sources search <term>'.[/dim]")
            return
        limit = getattr(args, "limit", None) or 0
        if limit > 0:
            all_ids = all_ids[:limit]

        title = f"Aidoku sources — {len(installed)} installed, {len(all_ids)} shown"
        if filtering:
            title += f" (filtered from {total_before})"
        table = Table(title=title)
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="white", overflow="fold")
        table.add_column("Lang", style="green", overflow="fold")
        table.add_column("Repo", style="yellow")
        table.add_column("Installed", style="magenta")

        def _lang_of(sid: str) -> str:
            return ",".join(_langs_of(sid)) or "?"

        if getattr(args, "json", False):
            _print_json([
                {
                    "id": sid,
                    "name": _name_of(sid),
                    "languages": _langs_of(sid),
                    "repo": _repo_of(sid),
                    "installed": sid in installed,
                }
                for sid in all_ids
            ])
            return
        for sid in all_ids:
            table.add_row(
                sid,
                _name_of(sid),
                _lang_of(sid),
                _repo_of(sid),
                "yes" if sid in installed else "",
            )
        console.print(table)
    elif action == "search":
        term = args.term.lower()
        if getattr(args, "index", None):
            entries = fetch_index_safe(index_url)[0] or []
            repo_by_id = {s.get("id"): args.index for s in entries}
            catalog_by_id = {s.get("id", ""): s for s in entries}
            unreachable = []
        else:
            agg, unreachable = aggregate_indexes()
            for r in unreachable:
                err_console.print(f"[yellow]Warning: repo '{r}' is unreachable.[/yellow]")
            repo_by_id = {sid: "+".join(r for r, _ in v) for sid, v in agg.items()}
            catalog_by_id = {sid: v[0][1] for sid, v in agg.items()}
        results = [
            (sid, catalog_by_id[sid])
            for sid in sorted(catalog_by_id)
            if term in catalog_by_id[sid].get("name", "").lower() or term in sid.lower()
        ]
        f_lang = (getattr(args, "lang", None) or "").strip().lower()
        f_repo = (getattr(args, "repo", None) or "").strip().lower()
        if f_lang:
            results = [
                (sid, entry) for sid, entry in results
                if any(f_lang == (lang or "").lower() for lang in (entry.get("languages") or []))
            ]
        if f_repo:
            results = [
                (sid, entry) for sid, entry in results
                if f_repo in repo_by_id.get(sid, "").lower()
            ]
        limit = getattr(args, "limit", None) or 0
        total = len(results)
        if limit > 0:
            results = results[:limit]
        if getattr(args, "json", False):
            _print_json([
                {
                    "id": sid,
                    "name": entry.get("name", "?"),
                    "languages": entry.get("languages") or [],
                    "repo": repo_by_id.get(sid, "?"),
                }
                for sid, entry in results
            ])
            return
        table = Table(title=f"Aidoku: '{args.term}' ({total})")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="white")
        table.add_column("Lang", style="green", overflow="fold")
        table.add_column("Repo", style="yellow")
        for sid, entry in results:
            table.add_row(
                sid,
                entry.get("name", "?"),
                ",".join(entry.get("languages") or []),
                repo_by_id.get(sid, "?"),
            )
        console.print(table)
    elif action == "install":
        from .aidoku_store import install

        ok, failed = 0, 0
        for sid in args.source_id:
            try:
                if getattr(args, "index", None):
                    install(sid, index_url=index_url)
                else:
                    found = find_source(sid)
                    if not found:
                        err_console.print(f"[red]'{sid}' not found in any repo.[/red] Try: wotoha sources search <term>")
                        failed += 1
                        continue
                    install(sid, index_url=found[1])
                console.print(f"[green]OK:[/green] {sid}")
                ok += 1
            except KeyError:
                err_console.print(f"[red]'{sid}' is not in the index.[/red] Try: wotoha sources search <term>")
                failed += 1
        if len(args.source_id) > 1:
            console.print(f"Installed {ok}/{len(args.source_id)}" + (f" ({failed} failed)" if failed else ""))
        if failed:
            sys.exit(1)
    elif action == "uninstall":
        from .aidoku_store import uninstall

        for sid in args.source_id:
            if uninstall(sid):
                console.print(f"[green]removed:[/green] {sid}")
            else:
                console.print(f"[yellow]not installed:[/yellow] {sid}")
    elif action == "repo":
        repo_action = getattr(args, "repo_action", None) or "list"
        if repo_action == "list":
            repos = list_repos()
            table = Table(title=f"Source repositories ({len(repos)})")
            table.add_column("Name", style="cyan", no_wrap=True)
            table.add_column("URL", style="white", overflow="fold")
            for name, url in repos.items():
                table.add_row(name, url)
            console.print(table)
            console.print("[dim]Usage: wotoha sources install --index <name|url> <source_id>[/dim]")
        elif repo_action == "add":
            add_repo(args.name, args.url)
            console.print(f"[green]OK:[/green] repo '{args.name}' -> {args.url}")
        elif repo_action == "remove":
            if remove_repo(args.name):
                console.print(f"[green]removed:[/green] {args.name}")
            else:
                err_console.print(f"[yellow]'{args.name}' is not registered.[/yellow]")
    elif action == "dir":
        console.print(sources_dir())


# --------------------------------------------------------------------------- #
# search / info / download (per-engine dispatch)
# --------------------------------------------------------------------------- #

def cmd_search(args) -> None:
    _set_language(args)
    if args.engine == "suwayomi":
        if not args.source:
            err_console.print("Use --source <id>.")
            sys.exit(2)
        data = _suwayomi_client().search(args.source, args.query, args.page)
        results_sw = data.get("mangaList", [])
        if getattr(args, "json", False):
            _print_json(results_sw)
            return
        print_manga_table(results_sw, f"Results for '{args.query}'", numbered=True)
        console.print("[dim]Hint: wotoha info <ID> for details. Use --pick to download interactively.[/dim]")
        if getattr(args, "pick", False):
            from .batch import pick_results as _pick

            chosen = _pick(results_sw)
            if not chosen:
                console.print("[yellow]Nothing selected.[/yellow]")
                return
            _download_picked(args, [str(m["id"]) for m in chosen], engine="suwayomi")
        return

    catalog = _aidoku_catalog()
    from .aidoku_store import list_installed, repo_of

    targets = [args.source] if args.source else [s["id"] for s in list_installed()]
    if not targets:
        err_console.print("No sources installed. Run: wotoha sources install <id>")
        sys.exit(1)

    results: list[dict] = []
    failed: list[tuple[str, str]] = []
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("Searching...", total=len(targets))
        for sid in targets:
            try:
                data = catalog.search(sid, args.query, args.page)
                for m in data["mangaList"]:
                    m["repo"] = repo_of(sid)
                results.extend(data["mangaList"])
            except Exception as e:  # noqa: BLE001
                failed.append((sid, str(e)[:80]))
                log.debug("source %s failed: %s", sid, e)
            finally:
                progress.advance(task)

    for sid, err in failed:
        err_console.print(f"[red]Failed {sid}:[/red] {err}")
    if failed and not results:
        err_console.print(
            "[yellow]Hint: the source may not be installed — "
            "run 'wotoha sources install <id>' or 'wotoha sources list'.[/yellow]"
        )
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    if getattr(args, "json", False):
        _print_json(results)
        return
    print_manga_table(results, f"Results for '{args.query}' ({len(results)})", numbered=True)
    console.print("[dim]Hint: wotoha info <ID> for details. Use --pick to download interactively.[/dim]")

    if getattr(args, "pick", False):
        from .batch import pick_results

        chosen = pick_results(results)
        if not chosen:
            console.print("[yellow]Nothing selected.[/yellow]")
            return
        _download_picked(args, [m["id"] for m in chosen], engine="aidoku")


def _download_picked(args, ids: list[str], engine: str) -> None:
    """Downloads --pick results honoring the user's -c/--mode/--pages/-f/-o.

    Only fills in defaults for selection flags the search command does not
    carry; never rewrites networking options behind the user's back.
    """
    scope = "latest chapter" if not getattr(args, "chapters", None) else f"chapters {args.chapters}"
    console.print(f"Downloading {scope} of {len(ids)} manga...")
    args.manga_id = ids
    args.batch_from_file = None
    args.pick = False
    if not getattr(args, "chapters", None):
        args.chapters = "latest"
    args.start_chapter = getattr(args, "start_chapter", None)
    args.end_chapter = getattr(args, "end_chapter", None)
    args.pages = getattr(args, "pages", None)
    args.no_oneshot = getattr(args, "no_oneshot", False)
    args.order = getattr(args, "order", "desc") or "desc"
    args.cover = getattr(args, "cover", False)
    args.use_volume_cover = getattr(args, "use_volume_cover", False)
    args.mode = getattr(args, "mode", "chapter") or "chapter"
    args.engine = engine
    args.workers = getattr(args, "workers", 4)
    args.retries = getattr(args, "retries", 3)
    args.delay_requests = getattr(args, "delay_requests", None)
    args.proxy = getattr(args, "proxy", None)
    args.dry_run = getattr(args, "dry_run", False)
    if engine == "suwayomi":
        _download_suwayomi(args)
    else:
        _download_aidoku(args)


def cmd_info(args) -> None:
    _set_language(args)
    show = getattr(args, "show_chapters", 5)
    if args.engine == "suwayomi":
        client = _suwayomi_client()
        full = client.get_manga_full(args.manga_id)
        chapters = client.get_chapters(args.manga_id)
        if getattr(args, "json", False):
            _print_json({"manga": full, "chapters": chapters})
            return
        console.print(f"[bold cyan]{full.get('title')}[/bold cyan]")
        if full.get("author"):
            console.print(f"Author: {full['author']}")
        if full.get("genre"):
            console.print(f"Genres: {', '.join(full['genre'])}")
        if full.get("description"):
            console.print(f"\n{full['description']}\n")
        console.print(f"Chapters: {len(chapters)}")
        if chapters and show != 0:
            console.print("\n[bold]Most recent:[/bold]")
            for c in chapters[:show]:
                num = c.get("chapterNumber")
                label = f"ch. {num:g}" if num is not None else c.get("name") or "?"
                console.print(f"  {c.get('index')} — {label}")
            if len(chapters) > show:
                console.print(f"  ... and {len(chapters) - show} more (use --show-chapters 0 for all)")
        elif chapters:
            console.print("\n[bold]Chapters:[/bold]")
            for c in chapters:
                num = c.get("chapterNumber")
                label = f"ch. {num:g}" if num is not None else c.get("name") or "?"
                console.print(f"  {c.get('index')} — {label}")
        return

    catalog = _aidoku_catalog()
    d = catalog.manga_details(args.manga_id)
    if getattr(args, "json", False):
        _print_json(d)
        return
    console.print(f"[bold cyan]{d['title']}[/bold cyan] [dim]({d['sourceId']})[/dim]")
    meta = []
    if d.get("authors"):
        meta.append(f"Author: {', '.join(d['authors'])}")
    if d.get("artists") and set(d["artists"]) - set(d.get("authors") or []):
        meta.append(f"Artist: {', '.join(d['artists'])}")
    if d.get("status"):
        meta.append(f"Status: {d['status']}")
    if d.get("url"):
        meta.append(f"Link: {d['url']}")
    if d.get("tags"):
        meta.append(f"Tags: {', '.join(t.strip() for t in d['tags'])}")
    for line in meta:
        console.print(line)
    if d.get("description"):
        console.print(f"\n{d['description']}\n")
    chapters = d.get("chapters") or []
    console.print(f"Chapters: {len(chapters)}")
    by_lang: dict[str, int] = {}
    locked_count = 0
    last_date = None
    for c in chapters:
        lang = c.get("language") or "?"
        by_lang[lang] = by_lang.get(lang, 0) + 1
        if c.get("locked"):
            locked_count += 1
        if c.get("dateUploaded"):
            last_date = max(last_date or 0, c["dateUploaded"])
    if len(by_lang) > 1 or (by_lang and next(iter(by_lang)) != "?"):
        dist = ", ".join(f"{k}: {v}" for k, v in sorted(by_lang.items()))
        console.print(f"Languages: {dist}")
        if not getattr(args, "language", None):
            console.print("[dim]Hint: use -l/--language to filter (e.g. -l pt-BR).[/dim]")
    if locked_count:
        console.print(f"Locked: {locked_count}")
    if last_date:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(last_date, tz=timezone.utc)
        console.print(f"Latest chapter: {dt.strftime('%Y-%m-%d')}")
    if chapters:
        if show == 0:
            console.print("\n[bold]Chapters:[/bold]")
            listed = chapters
        else:
            console.print("\n[bold]Most recent:[/bold]")
            listed = chapters[:show]
        for c in listed:
            num = c.get("chapterNumber")
            label = f"ch. {num:g}" if num is not None else c.get("title") or "?"
            lang = c.get("language")
            suffix = f" [{lang}]" if lang and len(by_lang) > 1 else ""
            console.print(f"  {c['key']} — {label}{suffix}")
        if show != 0 and len(chapters) > show:
            console.print(f"  ... and {len(chapters) - show} more (use --show-chapters 0 for all)")


def _set_language(args) -> None:
    """--language overrides WOTOHA_LANGUAGES for the multi-language source filter."""
    lang = getattr(args, "language", None)
    if lang:
        config.languages = lang


def _apply_delay(args) -> None:
    """Applies --delay-requests from the CLI (global)."""
    delay = getattr(args, "delay_requests", None)
    if delay:
        from . import net

        config.rate_limit_calls = 1
        config.rate_limit_seconds = float(delay)
        net.reset_rate_limiter()
    proxy = getattr(args, "proxy", None)
    if proxy:
        config.proxy = proxy


def _resolve_mangas(args, catalog, downloader) -> list[dict]:
    """Resolves the list of manga to download (batch: multiple IDs or file)."""
    ids: list[str] = list(getattr(args, "manga_id", None) or [])
    batch_file = getattr(args, "batch_from_file", None) or getattr(args, "batch_file", None)
    if batch_file:
        ids = []
        try:
            text = Path(batch_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            err_console.print(f"[red]Batch file not found:[/red] {batch_file}")
            sys.exit(1)
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    if not ids:
        err_console.print("Provide ID(s) or use --batch-from-file.")
        sys.exit(2)
    mangas = []
    for mid in ids:
        d = catalog.manga_details(mid)
        d["id"] = mid
        mangas.append(d)
    return mangas


def _print_dry_run(details: dict, selected) -> None:
    table = Table(title=f"Dry run: {details.get('title')} ({len(selected)} chapter(s))")
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("Chapter", style="white", overflow="fold")
    table.add_column("Volume", style="green")
    table.add_column("Scanlators", style="yellow", overflow="fold")
    for i, c in enumerate(selected, start=1):
        vol = getattr(c, "volume_number", None)
        table.add_row(
            str(i),
            _chapter_label(c),
            f"{vol:g}" if vol is not None else "-",
            ", ".join(getattr(c, "scanlators", None) or ([getattr(c, "scanlator", None)] if getattr(c, "scanlator", None) else []) or []) or "-",
        )
    console.print(table)
    console.print("[dim]Nothing downloaded. Remove --dry-run to download.[/dim]")


def _cover_page_file(fetcher, composite_id: str, cover_url: str | None, cache_dir: Path) -> Path | None:
    """Downloads the manga cover once (cached in cache_dir) to be injected as
    page 0000 of each chapter workdir. Returns None on any failure."""
    if not cover_url:
        return None
    from .downloader import ChapterDownloader as _CD

    key = safe_name(composite_id)
    for f in sorted(cache_dir.glob(f"{key}-cover.*")):
        return f
    try:
        data = fetcher()
    except Exception as e:  # noqa: BLE001 - cover is best-effort
        console.print(f"[yellow]Cover page unavailable: {e}[/yellow]")
        return None
    out = cache_dir / f"{key}-cover{_CD._ext_from(data)}"
    out.write_bytes(data)
    return out


def _download_aidoku(args) -> None:
    from .native_downloader import NativeDownloader

    _apply_delay(args)
    _set_language(args)
    catalog = _aidoku_catalog()
    dl = NativeDownloader(catalog, workers=args.workers, retries=args.retries)

    mangas = _resolve_mangas(args, catalog, dl)

    mode = getattr(args, "mode", "chapter")
    fmt = get_format(args.format, config.download_dir, mode=mode)
    needs_files = not (
        isinstance(fmt, RawFormat) and mode == "chapter"
    )  # raw chapter renames the dir; other modes need real files
    base_tmp = config.download_dir / ".cache"

    total = len(mangas)
    all_results: list[tuple[str, str, str]] = []
    for mi, details in enumerate(mangas, start=1):
        composite_id = details["id"]
        if total > 1:
            console.print(f"[bold][{mi}/{total}][/] {details['title']}")
        chapters = dl.list_chapters(composite_id, no_oneshot=args.no_oneshot)
        if mode == "volume" and chapters and all(
            c.volume_number is None for c in chapters
        ):
            console.print(
                "[yellow]Warning: this source provides no volume information; "
                "volume mode packs every chapter into 'No Volume'. "
                "Consider --mode chapter.[/yellow]"
            )
        selected = dl.select_chapters(
            chapters,
            args.chapters,
            start_chapter=args.start_chapter,
            end_chapter=args.end_chapter,
        )
        if not selected:
            console.print("[yellow]No chapters selected.[/yellow]")
            continue

        if getattr(args, "dry_run", False):
            _print_dry_run(details, selected)
            continue
        base_tmp.mkdir(parents=True, exist_ok=True)

        manga_stub = {
            "title": details["title"],
            "id": composite_id,
            "url": details.get("url"),
            "authors": details.get("authors"),
            "artists": details.get("artists"),
            "description": details.get("description"),
            "tags": details.get("tags"),
            "lang": details.get("sourceId", "").split(".")[0] or None,
        }

        if getattr(args, "cover", False):
            dl.download_cover(composite_id, config.download_dir / safe_name(details["title"]))

        cover_file = None
        if getattr(args, "use_volume_cover", False):
            source_id = composite_id.split("::", 1)[0]
            cover_file = _cover_page_file(
                lambda: dl.catalog.fetch_page_image(source_id, details.get("cover")),
                composite_id, details.get("cover"), base_tmp,
            )

        order = selected[::-1] if args.order == "asc" else selected

        console.print(
            f"Downloading [bold cyan]{details['title']}[/bold cyan] — "
            f"{len(order)} chapter(s), format {args.format.upper()}, mode {mode}"
        )

        results: list[tuple[str, str, str]] = []
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Downloading...", total=len(order))
            seen_volumes: set[str] = set()
            try:
                for chapter in order:
                    progress.update(task, description=f"[cyan]{safe_name(chapter.name)[:40]}")
                    workdir = base_tmp / safe_name(f"{composite_id}-{chapter.key}")
                    if needs_files:
                        workdir.mkdir(parents=True, exist_ok=True)
                    try:
                        skipped = (workdir / ".complete").is_file() if needs_files else False
                        dl.download_chapter(composite_id, chapter, workdir, pages_spec=args.pages)
                    except Exception as e:  # noqa: BLE001 - one bad chapter shouldn't kill the run
                        label = "" if isinstance(e, MangaDlError) else f"{type(e).__name__}: "
                        err_console.print(f"[red]Failed {chapter.name}:[/red] {label}{e}")
                        results.append((chapter.name, "failed", str(e)[:60]))
                        shutil.rmtree(workdir, ignore_errors=True)
                        progress.advance(task)
                        continue
                    vol_key = _volume_folder_name(chapter)
                    first_of_group = vol_key not in seen_volumes
                    if cover_file is not None and (
                        mode == "chapter"
                        or (mode == "volume" and first_of_group)
                        or (mode == "single" and not seen_volumes)
                    ):
                        shutil.copy2(cover_file, workdir / f"0000{cover_file.suffix}")
                    seen_volumes.add(vol_key)
                    if mode == "volume":
                        fmt.save_volume(manga_stub, chapter, workdir)
                    else:
                        fmt.save_chapter(manga_stub, chapter, workdir)
                    if needs_files:
                        shutil.rmtree(workdir, ignore_errors=True)
                    results.append((chapter.name, "skipped" if skipped else "ok", ""))
                    progress.advance(task)
            except KeyboardInterrupt:
                # let the progress bar render one last time; the ThreadPoolExecutor
                # atexit hook would otherwise spew join noise on shutdown
                err_console.print(
                    "\n[yellow]Interrupted — partial files kept. "
                    "Re-run the same command to resume.[/yellow]"
                )
                raise SystemExit(130) from None

        fmt.finish(manga_stub)
        _print_download_summary(results)
        all_results.extend(results)
    if getattr(args, "dry_run", False):
        return
    console.print("[green]Done.[/green] Output at:", config.download_dir.resolve())
    if all_results and all(s != "ok" and s != "skipped" for _, s, _ in all_results):
        err_console.print("[yellow]Hint: all chapters failed. Re-run later — flaky image CDNs recover over time.[/yellow]")


def _download_suwayomi(args) -> None:
    from .client import SuwayomiClient
    from .downloader import ChapterDownloader

    _apply_delay(args)
    client = SuwayomiClient()
    batch_file = getattr(args, "batch_from_file", None)
    if batch_file:
        err_console.print("[red]--batch-from-file is only supported by the aidoku engine.[/red]")
        sys.exit(2)
    ids = list(getattr(args, "manga_id", None) or [])
    if not ids:
        err_console.print("Provide ID(s).")
        sys.exit(2)
    manga_id = int(ids[0])  # suwayomi uses a single numeric id
    manga = client.get_manga(manga_id)
    dl = ChapterDownloader(client, workers=args.workers, retries=args.retries)
    chapters = dl.list_chapters(manga_id, no_oneshot=args.no_oneshot)
    selected = dl.select_chapters(
        chapters,
        args.chapters,
        start_chapter=args.start_chapter,
        end_chapter=args.end_chapter,
    )
    if not selected:
        console.print("[yellow]No chapters selected.[/yellow]")
        return

    if getattr(args, "dry_run", False):
        _print_dry_run({"title": manga.get("title") or f"Manga {manga_id}"}, selected)
        return
    base_tmp.mkdir(parents=True, exist_ok=True)

    mode = getattr(args, "mode", "chapter")
    fmt = get_format(args.format, config.download_dir, mode=mode)
    needs_files = not (isinstance(fmt, RawFormat) and mode == "chapter")
    base_tmp = config.download_dir / ".cache"

    manga_meta = {
        "title": manga.get("title") or f"Manga {manga_id}",
        "id": manga.get("id", manga_id),
        "url": manga.get("realUrl") or manga.get("url"),
        "author": manga.get("author"),
        "authors": [manga["author"]] if manga.get("author") else None,
        "description": manga.get("description"),
        "tags": manga.get("genre"),
    }

    cover_file = None
    if getattr(args, "use_volume_cover", False):
        try:
            data = client.get_manga_thumbnail(manga_id)
            from .downloader import ChapterDownloader as _CD

            cover_file = base_tmp / f"{manga_id}-cover{_CD._ext_from(data)}"
            cover_file.write_bytes(data)
        except Exception as e:  # noqa: BLE001 - cover is best-effort
            console.print(f"[yellow]Cover page unavailable: {e}[/yellow]")

    if getattr(args, "cover", False):
        data = client.get_manga_thumbnail(manga_id)
        cover_dir = config.download_dir / safe_name(manga_meta["title"])
        cover_dir.mkdir(parents=True, exist_ok=True)
        from .downloader import ChapterDownloader as _CD

        (cover_dir / f"cover.jpg").write_bytes(data)

    order = selected[::-1] if args.order == "asc" else selected

    console.print(
        f"Downloading [bold cyan]{manga_meta['title']}[/bold cyan] — "
        f"{len(order)} chapter(s), format {args.format.upper()}, mode {mode}"
    )

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
    ) as progress:
        task = progress.add_task("Downloading...", total=len(order))
        seen_volumes: set[str] = set()
        results: list[tuple[str, str, str]] = []
        for chapter in order:
            progress.update(task, description=f"[cyan]{safe_name(chapter.name)[:40]}")
            workdir = base_tmp / safe_name(f"{manga_id}-{chapter.index}")
            if needs_files:
                workdir.mkdir(parents=True, exist_ok=True)
            try:
                dl.download_chapter(manga, chapter, workdir, pages_spec=args.pages)
                vol_key = _volume_folder_name(chapter)
                first_of_group = vol_key not in seen_volumes
                if cover_file is not None and (
                    mode == "chapter"
                    or (mode == "volume" and first_of_group)
                    or (mode == "single" and not seen_volumes)
                ):
                    shutil.copy2(cover_file, workdir / f"0000{cover_file.suffix}")
                seen_volumes.add(vol_key)
                if mode == "volume":
                    fmt.save_volume(manga_meta, chapter, workdir)
                else:
                    fmt.save_chapter(manga_meta, chapter, workdir)
                results.append((chapter.name, "ok", ""))
            except Exception as e:  # noqa: BLE001 - one bad chapter shouldn't kill the run
                label = "" if isinstance(e, MangaDlError) else f"{type(e).__name__}: "
                err_console.print(f"[red]Failed {chapter.name}:[/red] {label}{e}")
                results.append((chapter.name, "failed", str(e)[:60]))
                shutil.rmtree(workdir, ignore_errors=True)
                progress.advance(task)
                continue
            if needs_files:
                shutil.rmtree(workdir, ignore_errors=True)
            progress.advance(task)

    fmt.finish(manga_meta)
    _print_download_summary(results)
    console.print("[green]Done.[/green] Output at:", config.download_dir.resolve())


def cmd_download(args) -> None:
    if args.engine == "suwayomi":
        _download_suwayomi(args)
    else:
        _download_aidoku(args)


def _chapter_number_from_name(name: str) -> float | None:
    import re as _re

    m = _re.search(r"(\d+(?:\.\d+)?)", name)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def cmd_convert(args) -> None:
    """Repackages downloaded chapters (raw folders) into cbz/epub/pdf/cb7.

    Each input may be a manga folder (subfolders with images) or a single
    chapter folder (images directly). No network access — engine-independent.
    """
    from types import SimpleNamespace

    from .formats import image_files

    inputs: list[Path] = [Path(p).expanduser() for p in args.input]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        err_console.print(f"[red]Not found:[/red] {', '.join(missing)}")
        sys.exit(1)
    out_base = Path(args.output).expanduser() if args.output else config.download_dir
    mode = getattr(args, "mode", "chapter")
    fmt = get_format(args.format, out_base, mode=mode)

    total_ok, total_failed = 0, 0
    for src in inputs:
        if src.is_file():
            err_console.print(f"[red]Not a folder:[/red] {src} (pass chapter/manga folders)")
            total_failed += 1
            continue
        direct_images = image_files(src)
        if direct_images:
            groups = [(src.name, [src])]
        else:
            subdirs = sorted(p for p in src.iterdir() if p.is_dir() and image_files(p))
            if not subdirs:
                err_console.print(f"[yellow]No images in:[/yellow] {src}")
                total_failed += 1
                continue
            groups = [(p.name, [p]) for p in subdirs]
        title = getattr(args, "title", None) or src.name
        manga_stub = {"title": title, "id": title, "url": None,
                      "authors": None, "artists": None,
                      "description": None, "tags": None, "lang": None}
        results: list[tuple[str, str, str]] = []
        for chap_name, _ in groups:
            chap_dir = src / chap_name if direct_images else next(p for p in src.iterdir() if p.name == chap_name)
            chapter = SimpleNamespace(
                name=chap_name,
                chapter_number=_chapter_number_from_name(chap_name),
                volume_number=None,
                title=None,
                date_uploaded=None,
            )
            try:
                if mode == "volume":
                    fmt.save_volume(manga_stub, chapter, chap_dir)
                else:
                    fmt.save_chapter(manga_stub, chapter, chap_dir)
                results.append((chap_name, "ok", ""))
                total_ok += 1
            except Exception as e:  # noqa: BLE001 - one bad folder shouldn't kill the run
                label = "" if isinstance(e, MangaDlError) else f"{type(e).__name__}: "
                err_console.print(f"[red]Failed {chap_name}:[/red] {label}{e}")
                results.append((chap_name, "failed", str(e)[:60]))
                total_failed += 1
        fmt.finish(manga_stub)
        _print_download_summary(results)
    console.print(f"[green]Converted {total_ok} chapter(s)[/green]" + (f" ({total_failed} failed)" if total_failed else "") + f". Output at: {out_base.resolve()}")
    if total_failed and not total_ok:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wotoha",
        description="Manga downloader CLI with Aidoku (WASM) extensions — lightweight, Java-free.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha sources list --installed-only\n"
               "  wotoha search \"solo leveling\" --source en.asurascans\n"
               "  wotoha info en.asurascans::solo-leveling\n"
               "  wotoha download en.asurascans::solo-leveling -f epub\n"
               "  wotoha convert ./My-Manga -f cbz\n"
               "Exit codes: 0 ok, 1 error, 130 interrupted (Ctrl+C).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logs")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # server / extensions (legacy suwayomi)
    p_server = sub.add_parser("server", help="[suwayomi] Manage the Suwayomi-Server")
    p_server.add_argument(
        "action", choices=["download", "start", "stop", "status"],
        help="download the jar, start/stop the server, or show its status",
    )
    p_server.add_argument("--force", action="store_true", help="re-download even if the jar exists")
    p_server.set_defaults(func=cmd_server)

    p_ext = sub.add_parser("extensions", aliases=["ext"], help="[suwayomi] Mihon extensions")
    ext_sub = p_ext.add_subparsers(dest="ext_action", required=True)
    ext_sub.add_parser("list", help="List available/installed extensions")
    p_i = ext_sub.add_parser("install", help="Install extension(s) by package name")
    p_i.add_argument("pkg", nargs="+")
    p_u = ext_sub.add_parser("uninstall", help="Remove extension(s)")
    p_u.add_argument("pkg", nargs="+")
    p_ext.set_defaults(func=cmd_extensions)

    # sources
    p_src = sub.add_parser(
        "sources", help="Manage Aidoku sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha sources list --installed-only\n"
               "  wotoha sources list --filter asura --lang en\n"
               "  wotoha sources search asura --limit 10\n"
               "  wotoha sources install en.asurascans\n",
    )
    src_sub = p_src.add_subparsers(dest="source_action")
    # alternative index (third-party repos), accepted by list/search/install/uninstall
    # and by 'sources' itself (SUPPRESS on subcommands so it doesn't override the parent)
    _index_parent = argparse.ArgumentParser(add_help=False)
    _index_parent.add_argument(
        "--index", default=argparse.SUPPRESS, metavar="NAME|URL",
        help="alternative Aidoku source index (third-party repos, e.g. "
             "https://smexhy.github.io/yomu-aidoku-sources/index.min.json)",
    )
    _json_parent = argparse.ArgumentParser(add_help=False)
    _json_parent.add_argument(
        "--json", action="store_true",
        help="machine-readable JSON output instead of the table",
    )
    _filter_parent = argparse.ArgumentParser(add_help=False)
    _filter_parent.add_argument(
        "--lang", default=None, metavar="LANG",
        help="only sources offering this language (e.g. en, pt-BR)",
    )
    _filter_parent.add_argument(
        "--repo", default=None, metavar="NAME",
        help="only sources from this repository (e.g. yomu)",
    )
    p_src_list = src_sub.add_parser(
        "list", help="List installed + available sources",
        parents=[_index_parent, _json_parent, _filter_parent])
    p_src_list.add_argument("--installed-only", action="store_true", help="show only installed sources")
    p_src_list.add_argument("--limit", type=int, default=0, metavar="N", help="show at most N sources (0 = all)")
    p_src_list.add_argument(
        "--filter", default=None, metavar="TEXT",
        help="only sources whose ID or name contains TEXT (case-insensitive)",
    )
    p_ss = src_sub.add_parser(
        "search", help="Search the indexes",
        parents=[_index_parent, _json_parent, _filter_parent])
    p_ss.add_argument("term")
    p_ss.add_argument("--limit", type=int, default=0, metavar="N", help="show at most N sources (0 = all)")
    p_si = src_sub.add_parser("install", help="Install source(s)", parents=[_index_parent])
    p_si.add_argument("source_id", nargs="+")
    p_su = src_sub.add_parser("uninstall", help="Remove source(s)", parents=[_index_parent])
    p_su.add_argument("source_id", nargs="+")
    src_sub.add_parser("dir", help="Show the sources folder")
    p_repo = src_sub.add_parser("repo", help="Manage source repositories")
    repo_sub = p_repo.add_subparsers(dest="repo_action")
    repo_sub.add_parser("list", help="List registered repositories")
    p_ra = repo_sub.add_parser("add", help="Register a repository")
    p_ra.add_argument("name", help="short name (e.g. yomu)")
    p_ra.add_argument("url", help="URL of the index.min.json")
    p_rr = repo_sub.add_parser("remove", help="Remove a repository")
    p_rr.add_argument("name", help="repository name")
    p_src.add_argument(
        "--index", default=None, metavar="NAME|URL",
        help="alternative Aidoku source index (third-party repos)",
    )
    p_src.add_argument(
        "--engine", default="aidoku", choices=["aidoku", "suwayomi"],
        help="backend engine (default: aidoku)",
    )
    p_src.set_defaults(func=cmd_sources)

    # search
    p_search = sub.add_parser(
        "search", help="Search manga",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha search \"solo leveling\" --source en.asurascans\n"
               "  wotoha search \"berserk\" --pick\n"
               "  wotoha search \"onimai\" -l pt-BR --pick -c 1-10 -f epub\n",
    )
    p_search.add_argument("query")
    p_search.add_argument("--source", help="source ID (default: all installed)")
    p_search.add_argument("--page", type=int, default=1, help="results page number (default: 1)")
    p_search.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="language for the sources' chapter filter (e.g. en, pt-BR); "
             "default: WOTOHA_LANGUAGES",
    )
    p_search.add_argument(
        "--engine", default="aidoku", choices=["aidoku", "suwayomi"],
        help="backend engine (default: aidoku)",
    )
    p_search.add_argument(
        "--json", action="store_true", help="machine-readable JSON output (skips --pick)",
    )
    # --pick downloads straight from results; the flags below only apply with --pick
    pick_group = p_search.add_argument_group("pick (download selected results)")
    pick_group.add_argument(
        "--pick", action="store_true",
        help="select results interactively and download (default: latest chapter; override with -c)",
    )
    pick_group.add_argument(
        "--chapters", "-c", default=None,
        help='chapters to download with --pick: "latest" (default), "all", or ranges like "1-10,15"',
    )
    pick_group.add_argument(
        "--mode", choices=["chapter", "volume", "single"], default="chapter",
        help="packaging with --pick (default: chapter)",
    )
    pick_group.add_argument("--pages", default=None, help='page range with --pick (e.g. "1-10")')
    pick_group.add_argument("--no-oneshot", action="store_true", help="skip oneshot chapters (with --pick)")
    pick_group.add_argument("--order", choices=["desc", "asc"], default="desc", help="download order (default: newest first)")
    pick_group.add_argument("--cover", action="store_true", help="download the manga cover (with --pick)")
    pick_group.add_argument("--use-volume-cover", action="store_true", help="cover as first page (with --pick)")
    pick_group.add_argument("--dry-run", action="store_true", help="list selected chapters without downloading (with --pick)")
    pick_group.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    pick_group.add_argument("--workers", type=int, default=4, help="parallel page downloads (default: %(default)s)")
    pick_group.add_argument("--retries", type=int, default=3, help="retry attempts per page (default: %(default)s)")
    pick_group.add_argument("--delay-requests", type=float, default=None, metavar="SEC", help="minimum interval between requests")
    pick_group.add_argument("--proxy", default=None, help="HTTP/SOCKS proxy (e.g. socks5://localhost:9050)")
    pick_group.add_argument("-o", "--output", default=None, metavar="DIR",
                            help="download directory (default: current directory or MANGA_DL_DIR)")
    p_search.set_defaults(func=cmd_search)

    # info
    p_info = sub.add_parser(
        "info", help="Manga details",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha info en.asurascans::solo-leveling\n"
               "  wotoha info multi.mangadex::<uuid> -l pt-BR --show-chapters 0\n",
    )
    p_info.add_argument("manga_id", help="ID returned by search (sourceId::key)")
    p_info.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="language for the sources' chapter filter (e.g. en, pt-BR); "
             "default: WOTOHA_LANGUAGES",
    )
    p_info.add_argument(
        "--show-chapters", type=int, default=5, metavar="N",
        help="recent chapters to list (default: 5; 0 = all)",
    )
    p_info.add_argument(
        "--json", action="store_true", help="machine-readable JSON output",
    )
    p_info.add_argument(
        "--engine", default="aidoku", choices=["aidoku", "suwayomi"],
        help="backend engine (default: aidoku)",
    )
    p_info.set_defaults(func=cmd_info)

    # download
    p_dl = sub.add_parser(
        "download", help="Download chapters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha download en.asurascans::solo-leveling -f epub\n"
               "  wotoha download en.asurascans::solo-leveling -c latest -f cbz\n"
               "  wotoha download multi.mangadex::<uuid> -l pt-BR -c 1-10 --dry-run\n"
               "  wotoha download --batch-from-file list.txt -f cbz\n",
    )
    p_dl.add_argument("manga_id", nargs="*", help="ID(s) from search (or use --batch-from-file)")
    sel = p_dl.add_argument_group("selection")
    sel.add_argument(
        "--chapters", "-c", default=None,
        help='chapters to download: "all" (default), "latest", or ranges like "1-10,15"',
    )
    sel.add_argument("--start-chapter", type=float, default=None, help="first chapter number (inclusive)")
    sel.add_argument("--end-chapter", type=float, default=None, help="last chapter number (inclusive)")
    sel.add_argument(
        "--pages", default=None,
        help='page range: "2", "1-10", "5-" (from 5 on), "3--" (last 3), "-5" (drop last 5)',
    )
    sel.add_argument("--no-oneshot", action="store_true", help="skip oneshot chapters")
    sel.add_argument("--dry-run", action="store_true", help="list selected chapters without downloading")
    out = p_dl.add_argument_group("output")
    out.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    out.add_argument(
        "--mode",
        choices=["chapter", "volume", "single"],
        default="chapter",
        help="packaging: per chapter (default), per volume, or a single file",
    )
    out.add_argument(
        "--order", choices=["desc", "asc"], default="desc",
        help="download order (default: newest first)",
    )
    out.add_argument("--cover", action="store_true", help="download the manga cover")
    out.add_argument(
        "--use-volume-cover",
        action="store_true",
        help="add the manga cover as the first page of each chapter/volume/file",
    )
    out.add_argument(
        "-o", "--output", default=None, metavar="DIR",
        help="download directory (default: current directory or MANGA_DL_DIR)",
    )
    net = p_dl.add_argument_group("network")
    net.add_argument(
        "--workers", type=int, default=4,
        help="parallel page downloads per chapter (default: %(default)s)",
    )
    net.add_argument(
        "--retries", type=int, default=3,
        help="retry attempts per page (default: %(default)s)",
    )
    net.add_argument(
        "--delay-requests", type=float, default=None, metavar="SEC",
        help="minimum interval between requests (rate limit)",
    )
    net.add_argument("--proxy", default=None, help="HTTP/SOCKS proxy (e.g. socks5://localhost:9050)")
    p_dl.add_argument("--batch-from-file", default=None, metavar="FILE",
                      help="file with one ID per line ('#' comments, empty lines ignored)")
    p_dl.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="chapter language on multi-language sources (e.g. en, pt-BR); "
             "default: WOTOHA_LANGUAGES",
    )
    p_dl.add_argument(
        "--engine", default="aidoku", choices=["aidoku", "suwayomi"],
        help="backend engine (default: aidoku)",
    )
    p_dl.set_defaults(func=cmd_download)

    # convert (offline repackaging, engine-independent)
    p_cv = sub.add_parser(
        "convert", help="Repackage downloaded folders (raw) into cbz/epub/pdf/cb7",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  wotoha convert ./My-Manga -f cbz\n"
               "  wotoha convert ./My-Manga -f epub --mode single -o ./out\n"
               "  wotoha convert ./A ./B -f pdf\n",
    )
    p_cv.add_argument("input", nargs="+", metavar="DIR", help="manga or chapter folder(s) with images")
    p_cv.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    p_cv.add_argument(
        "--mode", choices=["chapter", "volume", "single"], default="chapter",
        help="packaging: per chapter (default), per volume, or a single file",
    )
    p_cv.add_argument("--title", default=None, help="manga title (default: folder name)")
    p_cv.add_argument(
        "-o", "--output", default=None, metavar="DIR",
        help="output directory (default: current directory or MANGA_DL_DIR)",
    )
    p_cv.set_defaults(func=cmd_convert)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if getattr(args, "output", None):
        config.download_dir = Path(args.output).expanduser()

    try:
        args.func(args)
    except MangaDlError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        err_console.print(f"[red]HTTP error {status}:[/red] {e}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        err_console.print(f"[red]Network error:[/red] {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - never traceback at users
        if args.verbose:
            raise
        err_console.print(f"[red]Error:[/red] {type(e).__name__}: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
