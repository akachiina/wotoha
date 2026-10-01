"""wotoha CLI.

Engines:
- aidoku (default): native WASM runtime, no Java
- suwayomi: legacy backend via Suwayomi-Server (optional)
"""

from __future__ import annotations

import argparse
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
from .formats import RawFormat, get_format

console = Console()
err_console = Console(stderr=True)

log = logging.getLogger("wotoha")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def print_manga_table(mangas: list[dict], title: str, id_key: str = "id") -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("ID", style="cyan", overflow="fold")
    table.add_column("Title", style="white", overflow="fold")
    if mangas and mangas[0].get("sourceId"):
        table.add_column("Source", style="green")
    if any(m.get("repo") for m in mangas):
        table.add_column("Repo", style="yellow")
    table.add_column("Status", style="magenta")
    for m in mangas:
        row = [str(m.get(id_key, "?")), m.get("title") or "?"]
        if mangas and mangas[0].get("sourceId"):
            row.append(str(m.get("sourceId")))
        if any(m.get("repo") for m in mangas):
            row.append(m.get("repo") or "?")
        row.append((m.get("status") or "?").title())
        table.add_row(*row)
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
        else:
            available, unreachable = aggregate_indexes()
            for r in unreachable:
                err_console.print(f"[yellow]⚠ repo '{r}' is unreachable.[/yellow]")
        all_ids = sorted(set(installed) | set(available))
        table = Table(title=f"Aidoku sources — {len(installed)} installed, {len(all_ids)} available")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="white", overflow="fold")
        table.add_column("Lang", style="green", overflow="fold")
        table.add_column("Repo", style="yellow")
        table.add_column("Installed", style="magenta")

        def _lang_of(sid: str) -> str:
            if sid in available:
                return ",".join(available[sid][0][1].get("languages") or []) or "?"
            entry = next((e for e in installed_entries if e["id"] == sid), None)
            return ",".join((entry or {}).get("languages") or []) or "?"

        for sid in all_ids:
            repos_of = available.get(sid)
            repo_label = "+".join(r for r, _ in repos_of) if repos_of else "?"
            table.add_row(
                sid,
                (repos_of[0][1].get("name") if repos_of else next((e["name"] for e in installed_entries if e["id"] == sid), "?")),
                _lang_of(sid),
                repo_label,
                "yes" if sid in installed else "",
            )
        console.print(table)
    elif action == "search":
        term = args.term.lower()
        if getattr(args, "index", None):
            entries = fetch_index_safe(index_url)[0] or []
            repo_by_id = {s.get("id"): args.index for s in entries}
            catalog_by_id = {s.get("id", ""): s for s in entries}
        else:
            agg, unreachable = aggregate_indexes()
            for r in unreachable:
                err_console.print(f"[yellow]⚠ repo '{r}' is unreachable.[/yellow]")
            repo_by_id = {sid: "+".join(r for r, _ in v) for sid, v in agg.items()}
            catalog_by_id = {sid: v[0][1] for sid, v in agg.items()}
        results = [
            (sid, catalog_by_id[sid])
            for sid in sorted(catalog_by_id)
            if term in catalog_by_id[sid].get("name", "").lower() or term in sid.lower()
        ]
        table = Table(title=f"Aidoku: '{args.term}' ({len(results)})")
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

        for sid in args.source_id:
            try:
                if getattr(args, "index", None):
                    install(sid, index_url=index_url)
                else:
                    found = find_source(sid)
                    if not found:
                        err_console.print(f"[red]'{sid}' not found in any repo.[/red] Try: wotoha sources search <term>")
                        sys.exit(1)
                    install(sid, index_url=found[1])
                console.print(f"[green]OK:[/green] {sid}")
            except KeyError:
                err_console.print(f"[red]'{sid}' is not in the index.[/red] Try: wotoha sources search <term>")
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
        print_manga_table(data.get("mangaList", []), f"Results for '{args.query}'")
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
        err_console.print(f"[red]✗ {sid}:[/red] {err}")
    if failed and not results:
        err_console.print(
            "[yellow]Hint: the source may not be installed — "
            "run 'wotoha sources install <id>' or 'wotoha sources list'.[/yellow]"
        )
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    print_manga_table(results, f"Results for '{args.query}' ({len(results)})")
    console.print("[dim]Hint: wotoha info <ID> for details.[/dim]")

    if getattr(args, "pick", False):
        from .batch import pick_results

        chosen = pick_results(results)
        if not chosen:
            console.print("[yellow]Nothing selected.[/yellow]")
            return
        ids = [m["id"] for m in chosen]
        console.print(f"Downloading {len(ids)} manga...")
        args.manga_id = ids
        args.batch_from_file = None
        args.pick = False
        args.chapters = "latest"
        args.start_chapter = args.end_chapter = None
        args.pages = None
        args.no_oneshot = False
        args.order = "desc"
        args.cover = False
        args.mode = "chapter"
        args.engine = "aidoku"
        args.workers = getattr(args, "workers", 4)
        args.retries = getattr(args, "retries", 3)
        args.delay_requests = None
        args.proxy = None
        _download_aidoku(args)


def cmd_info(args) -> None:
    _set_language(args)
    if args.engine == "suwayomi":
        client = _suwayomi_client()
        full = client.get_manga_full(args.manga_id)
        chapters = client.get_chapters(args.manga_id)
        console.print(f"[bold cyan]{full.get('title')}[/bold cyan]")
        if full.get("author"):
            console.print(f"Author: {full['author']}")
        if full.get("genre"):
            console.print(f"Genres: {', '.join(full['genre'])}")
        if full.get("description"):
            console.print(f"\n{full['description']}\n")
        console.print(f"Chapters: {len(chapters)}")
        return

    catalog = _aidoku_catalog()
    d = catalog.manga_details(args.manga_id)
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
    if locked_count:
        console.print(f"Locked: {locked_count}")
    if last_date:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(last_date, tz=timezone.utc)
        console.print(f"Latest chapter: {dt.strftime('%Y-%m-%d')}")
    if chapters:
        console.print("\n[bold]Most recent:[/bold]")
        for c in chapters[:5]:
            num = c.get("chapterNumber")
            label = f"ch. {num:g}" if num is not None else c.get("title") or "?"
            lang = c.get("language")
            suffix = f" [{lang}]" if lang and len(by_lang) > 1 else ""
            console.print(f"  {c['key']} — {label}{suffix}")
        if len(chapters) > 5:
            console.print(f"  ... and {len(chapters) - 5} more")


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
    ids: list[str] = list(args.manga_id or [])
    if getattr(args, "batch_file", None):
        ids = []
        for line in Path(args.batch_file).read_text(encoding="utf-8").splitlines():
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
    base_tmp.mkdir(parents=True, exist_ok=True)

    total = len(mangas)
    for mi, details in enumerate(mangas, start=1):
        composite_id = details["id"]
        if total > 1:
            console.print(f"[bold][{mi}/{total}][/] {details['title']}")
        chapters = dl.list_chapters(composite_id, no_oneshot=args.no_oneshot)
        selected = dl.select_chapters(
            chapters,
            args.chapters,
            start_chapter=args.start_chapter,
            end_chapter=args.end_chapter,
        )
        if not selected:
            console.print("[yellow]No chapters selected.[/yellow]")
            continue

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

        order = selected[::-1] if args.order == "asc" else selected

        console.print(
            f"Downloading [bold cyan]{details['title']}[/bold cyan] — "
            f"{len(order)} chapter(s), format {args.format.upper()}, mode {mode}"
        )

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Downloading...", total=len(order))
            for chapter in order:
                progress.update(task, description=f"[cyan]{safe_name(chapter.name)[:40]}")
                workdir = base_tmp / safe_name(f"{composite_id}-{chapter.key}")
                if needs_files:
                    workdir.mkdir(parents=True, exist_ok=True)
                dl.download_chapter(composite_id, chapter, workdir, pages_spec=args.pages)
                if mode == "volume":
                    fmt.save_volume(manga_stub, chapter, workdir)
                else:
                    fmt.save_chapter(manga_stub, chapter, workdir)
                if needs_files:
                    shutil.rmtree(workdir, ignore_errors=True)
                progress.advance(task)

        fmt.finish(manga_stub)
    console.print("[green]Done.[/green] Output at:", config.download_dir.resolve())


def _download_suwayomi(args) -> None:
    from .client import SuwayomiClient
    from .downloader import ChapterDownloader

    _apply_delay(args)
    client = SuwayomiClient()
    manga_id = int(args.manga_id[0])  # suwayomi uses a single numeric id
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

    mode = getattr(args, "mode", "chapter")
    fmt = get_format(args.format, config.download_dir, mode=mode)
    needs_files = not (isinstance(fmt, RawFormat) and mode == "chapter")
    base_tmp = config.download_dir / ".cache"
    base_tmp.mkdir(parents=True, exist_ok=True)

    manga_meta = {
        "title": manga.get("title") or f"Manga {manga_id}",
        "id": manga.get("id", manga_id),
        "url": manga.get("realUrl") or manga.get("url"),
        "author": manga.get("author"),
        "authors": [manga["author"]] if manga.get("author") else None,
        "description": manga.get("description"),
        "tags": manga.get("genre"),
    }

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
        for chapter in order:
            progress.update(task, description=f"[cyan]{safe_name(chapter.name)[:40]}")
            workdir = base_tmp / safe_name(f"{manga_id}-{chapter.index}")
            if needs_files:
                workdir.mkdir(parents=True, exist_ok=True)
            dl.download_chapter(manga, chapter, workdir, pages_spec=args.pages)
            if mode == "volume":
                fmt.save_volume(manga_meta, chapter, workdir)
            else:
                fmt.save_chapter(manga_meta, chapter, workdir)
            if needs_files:
                shutil.rmtree(workdir, ignore_errors=True)
            progress.advance(task)

    fmt.finish(manga_meta)
    console.print("[green]Done.[/green] Output at:", config.download_dir.resolve())


def cmd_download(args) -> None:
    if args.engine == "suwayomi":
        _download_suwayomi(args)
    else:
        _download_aidoku(args)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wotoha",
        description="Manga downloader CLI with Aidoku (WASM) extensions — lightweight, Java-free.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logs")

    sub = parser.add_subparsers(dest="command", required=True)

    # server / extensions (legacy suwayomi)
    p_server = sub.add_parser("server", help="[suwayomi] Manage the Suwayomi-Server")
    p_server.add_argument("action", choices=["download", "start", "stop", "status"])
    p_server.add_argument("--force", action="store_true")
    p_server.set_defaults(func=cmd_server)

    p_ext = sub.add_parser("extensions", aliases=["ext"], help="[suwayomi] Mihon extensions")
    ext_sub = p_ext.add_subparsers(dest="ext_action", required=True)
    ext_sub.add_parser("list")
    p_i = ext_sub.add_parser("install"); p_i.add_argument("pkg", nargs="+")
    p_u = ext_sub.add_parser("uninstall"); p_u.add_argument("pkg", nargs="+")
    p_ext.set_defaults(func=cmd_extensions)

    # sources
    p_src = sub.add_parser("sources", help="Manage Aidoku sources")
    src_sub = p_src.add_subparsers(dest="source_action")
    # alternative index (third-party repos), accepted by list/search/install/uninstall
    # and by 'sources' itself (SUPPRESS on subcommands so it doesn't override the parent)
    _index_parent = argparse.ArgumentParser(add_help=False)
    _index_parent.add_argument(
        "--index", default=argparse.SUPPRESS, metavar="NAME|URL",
        help="alternative Aidoku source index (third-party repos, e.g. "
             "https://smexhy.github.io/yomu-aidoku-sources/index.min.json)",
    )
    src_sub.add_parser("list", help="List installed + available sources", parents=[_index_parent])
    p_ss = src_sub.add_parser("search", help="Search the indexes", parents=[_index_parent])
    p_ss.add_argument("term")
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
    p_rr.add_argument("name")
    p_src.add_argument(
        "--index", default=None, metavar="NAME|URL",
        help="alternative Aidoku source index (third-party repos)",
    )
    p_src.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_src.set_defaults(func=cmd_sources)

    # search
    p_search = sub.add_parser("search", help="Search manga")
    p_search.add_argument("query")
    p_search.add_argument("--source", help="source ID (default: all installed)")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="language for the sources' chapter filter (e.g. en, pt-BR); "
             "default: WOTOHA_LANGUAGES (current: %(default)s)",
    )
    p_search.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_search.set_defaults(func=cmd_search)

    # info
    p_info = sub.add_parser("info", help="Manga details")
    p_info.add_argument("manga_id", help="ID returned by search (sourceId::key)")
    p_info.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="language for the sources' chapter filter (e.g. en, pt-BR)",
    )
    p_info.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_info.set_defaults(func=cmd_info)

    # download
    p_dl = sub.add_parser("download", help="Download chapters")
    p_dl.add_argument("manga_id", nargs="+", help="ID(s) from search (batch: multiple IDs)")
    p_dl.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    p_dl.add_argument(
        "--mode",
        choices=["chapter", "volume", "single"],
        default="chapter",
        help="packaging: per chapter (default), per volume, or a single file",
    )
    p_dl.add_argument("--chapters", "-c", default=None, help='"all", "latest", "1-10,15"')
    p_dl.add_argument("--start-chapter", type=float, default=None)
    p_dl.add_argument("--end-chapter", type=float, default=None)
    p_dl.add_argument(
        "--pages", default=None,
        help='page range: "2", "1-10", "3--" (last 3)',
    )
    p_dl.add_argument("--no-oneshot", action="store_true", help="skip oneshot chapters")
    p_dl.add_argument(
        "--language", "-l", default=None, metavar="LANG",
        help="chapter language on multi-language sources (e.g. en, pt-BR); "
             "default: WOTOHA_LANGUAGES",
    )
    p_dl.add_argument(
        "--order", choices=["desc", "asc"], default="desc",
        help="download order (default: newest first)",
    )
    p_dl.add_argument("--cover", action="store_true", help="download the manga cover")
    p_dl.add_argument("--batch-from-file", default=None, metavar="FILE",
                      help="file with one ID per line")
    p_dl.add_argument("--workers", type=int, default=4)
    p_dl.add_argument("--retries", type=int, default=3)
    p_dl.add_argument(
        "--delay-requests", type=float, default=None, metavar="SEC",
        help="minimum interval between requests (rate limit)",
    )
    p_dl.add_argument("--proxy", default=None, help="HTTP/SOCKS proxy (e.g. socks5://localhost:9050)")
    p_dl.add_argument("-o", "--output", default=None)
    p_dl.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_dl.set_defaults(func=cmd_download)

    # serve (local web UI)
    p_serve = sub.add_parser("serve", help="Start the local web UI")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8321, help="port (default: 8321)")
    p_serve.add_argument("--open", action="store_true", help="open the browser automatically")
    p_serve.set_defaults(func=cmd_serve)

    # search gets --pick (downloads straight from results)
    p_search.add_argument(
        "--pick", action="store_true",
        help="select results interactively and download the latest chapter of each",
    )
    p_search.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    p_search.add_argument("--workers", type=int, default=4)
    p_search.add_argument("--retries", type=int, default=3)
    p_search.add_argument("-o", "--output", default=None)
    p_search.set_defaults(func=cmd_search)

    return parser


def cmd_serve(args) -> None:
    from .web import serve

    serve(host=args.host, port=args.port, open_browser=args.open)


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
    except KeyboardInterrupt:
        err_console.print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
