"""CLI do wotoha.

Engines:
- aidoku (default): runtime WASM nativo, sem Java
- suwayomi: backend legado via Suwayomi-Server (opcional)
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
    table.add_column("Título", style="white", overflow="fold")
    if mangas and mangas[0].get("sourceId"):
        table.add_column("Source", style="green")
    table.add_column("Status", style="magenta")
    for m in mangas:
        row = [str(m.get(id_key, "?")), m.get("title") or "?"]
        if mangas and mangas[0].get("sourceId"):
            row.append(str(m.get("sourceId")))
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
            f"[red]Suwayomi offline em {config.server_url}.[/red] "
            "Inicie com: [bold]wotoha server start[/bold]"
        )
        sys.exit(1)
    return client


# --------------------------------------------------------------------------- #
# server / extensions (suwayomi legado)
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
        table = Table(title=f"Extensões Suwayomi ({len(exts)})")
        table.add_column("pkgName", style="cyan", overflow="fold")
        table.add_column("Nome", style="white", overflow="fold")
        table.add_column("Lang", style="green")
        table.add_column("Instalada", style="magenta")
        for e in exts:
            table.add_row(
                e.get("pkgName") or "?",
                e.get("name") or "?",
                e.get("lang") or "?",
                "sim" if e.get("installed", e.get("isInstalled")) else "não",
            )
        console.print(table)
    elif args.ext_action == "install":
        for pkg in args.pkg:
            print(f"Instalando {pkg}...")
            client.install_extension(pkg)
            print(f"  OK: {pkg}")
    elif args.ext_action == "uninstall":
        for pkg in args.pkg:
            print(f"Desinstalando {pkg}...")
            client.uninstall_extension(pkg)
            print(f"  OK: {pkg}")


# --------------------------------------------------------------------------- #
# sources (aidoku nativo + suwayomi)
# --------------------------------------------------------------------------- #

def cmd_sources(args) -> None:
    if args.engine == "suwayomi":
        from .client import SuwayomiClient

        sources = SuwayomiClient().list_sources()
        table = Table(title=f"Sources Suwayomi ({len(sources)})")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Nome", style="white")
        table.add_column("Lang", style="green")
        for s in sources:
            table.add_row(s["id"], s.get("displayName") or s.get("name") or "?", s.get("lang") or "?")
        console.print(table)
        return

    from .aidoku_store import fetch_index, list_installed, search_index, sources_dir

    action = args.source_action or "list"
    if action == "list":
        installed = {s["id"] for s in list_installed()}
        try:
            available = fetch_index()
        except Exception as e:  # noqa: BLE001
            err_console.print(f"[yellow]Índice indisponível ({e}); mostrando só instalados.[/yellow]")
            available = []
        table = Table(title=f"Sources Aidoku — {len(installed)} instalados, {len(available)} disponíveis")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Nome", style="white", overflow="fold")
        table.add_column("Lang", style="green")
        table.add_column("Instalado", style="magenta")
        def _row(s, installed_flag):
            table.add_row(
                s.get("id", "?"),
                s.get("name", "?"),
                ",".join(s.get("languages") or []) or "?",
                "sim" if installed_flag else "",
            )
        for s in list_installed():
            _row(s, True)
        for s in available:
            if s.get("id") not in installed:
                _row(s, False)
        console.print(table)
    elif action == "search":
        results = search_index(args.term)
        table = Table(title=f"Aidoku: '{args.term}' ({len(results)})")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Nome", style="white")
        table.add_column("Lang", style="green")
        for s in results:
            table.add_row(s["id"], s.get("name", "?"), ",".join(s.get("languages") or []))
        console.print(table)
    elif action == "install":
        from .aidoku_store import install

        for sid in args.source_id:
            try:
                install(sid)
                console.print(f"[green]OK:[/green] {sid}")
            except KeyError:
                err_console.print(f"[red]'{sid}' não está no índice.[/red] Use: wotoha sources search <termo>")
                sys.exit(1)
    elif action == "uninstall":
        from .aidoku_store import uninstall

        for sid in args.source_id:
            if uninstall(sid):
                console.print(f"[green]removido:[/green] {sid}")
            else:
                console.print(f"[yellow]não instalado:[/yellow] {sid}")
    elif action == "dir":
        console.print(sources_dir())


# --------------------------------------------------------------------------- #
# search / info / download (dispatch por engine)
# --------------------------------------------------------------------------- #

def cmd_search(args) -> None:
    if args.engine == "suwayomi":
        if not args.source:
            err_console.print("Use --source <id>.")
            sys.exit(2)
        data = _suwayomi_client().search(args.source, args.query, args.page)
        print_manga_table(data.get("mangaList", []), f"Resultados para '{args.query}'")
        return

    catalog = _aidoku_catalog()
    from .aidoku_store import list_installed

    targets = [args.source] if args.source else [s["id"] for s in list_installed()]
    if not targets:
        err_console.print("Nenhum source instalado. Rode: wotoha sources install <id>")
        sys.exit(1)

    results: list[dict] = []
    failed: list[tuple[str, str]] = []
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("Buscando...", total=len(targets))
        for sid in targets:
            try:
                data = catalog.search(sid, args.query, args.page)
                results.extend(data["mangaList"])
            except Exception as e:  # noqa: BLE001
                failed.append((sid, str(e)[:80]))
                log.debug("source %s falhou: %s", sid, e)
            finally:
                progress.advance(task)

    for sid, err in failed:
        err_console.print(f"[red]✗ {sid}:[/red] {err}")
    if failed and not results:
        err_console.print(
            "[yellow]Dica: o source pode não estar instalado — "
            "rode 'wotoha sources install <id>' ou 'wotoha sources list'.[/yellow]"
        )
    if not results:
        console.print("[yellow]Nenhum resultado.[/yellow]")
        return
    print_manga_table(results, f"Resultados para '{args.query}' ({len(results)})")
    console.print("[dim]Dica: wotoha info <ID> para detalhes.[/dim]")

    if getattr(args, "pick", False):
        from .batch import pick_results

        chosen = pick_results(results)
        if not chosen:
            console.print("[yellow]Nada selecionado.[/yellow]")
            return
        ids = [m["id"] for m in chosen]
        console.print(f"Baixando {len(ids)} mangá(s)...")
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
    if args.engine == "suwayomi":
        client = _suwayomi_client()
        full = client.get_manga_full(args.manga_id)
        chapters = client.get_chapters(args.manga_id)
        console.print(f"[bold cyan]{full.get('title')}[/bold cyan]")
        if full.get("author"):
            console.print(f"Autor: {full['author']}")
        if full.get("genre"):
            console.print(f"Gêneros: {', '.join(full['genre'])}")
        if full.get("description"):
            console.print(f"\n{full['description']}\n")
        console.print(f"Capítulos: {len(chapters)}")
        return

    catalog = _aidoku_catalog()
    d = catalog.manga_details(args.manga_id)
    console.print(f"[bold cyan]{d['title']}[/bold cyan] [dim]({d['sourceId']})[/dim]")
    meta = []
    if d.get("authors"):
        meta.append(f"Autor: {', '.join(d['authors'])}")
    if d.get("status"):
        meta.append(f"Status: {d['status']}")
    if d.get("tags"):
        meta.append(f"Tags: {', '.join(t.strip() for t in d['tags'])}")
    for line in meta:
        console.print(line)
    if d.get("description"):
        console.print(f"\n{d['description']}\n")
    chapters = d.get("chapters") or []
    console.print(f"Capítulos: {len(chapters)}")
    if chapters:
        console.print("\n[bold]Mais recentes:[/bold]")
        for c in chapters[:5]:
            num = c.get("chapterNumber")
            label = f"cap. {num:g}" if num is not None else c.get("title") or "?"
            console.print(f"  {c['key']} — {label}")
        if len(chapters) > 5:
            console.print(f"  ... e mais {len(chapters) - 5}")


def _apply_delay(args) -> None:
    """Aplica --delay-requests configurado via CLI (global)."""
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
    """Resolve a lista de mangás a baixar (batch: IDs múltiplos ou arquivo)."""
    ids: list[str] = list(args.manga_id or [])
    if getattr(args, "batch_file", None):
        ids = []
        for line in Path(args.batch_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    if not ids:
        err_console.print("Informe ID(s) ou use --batch-from-file.")
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
    catalog = _aidoku_catalog()
    dl = NativeDownloader(catalog, workers=args.workers, retries=args.retries)

    mangas = _resolve_mangas(args, catalog, dl)

    mode = getattr(args, "mode", "chapter")
    fmt = get_format(args.format, config.download_dir, mode=mode)
    needs_files = not (
        isinstance(fmt, RawFormat) and mode == "chapter"
    )  # raw chapter rename o dir; outros modos precisam de arquivos
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
            console.print("[yellow]Nenhum capítulo selecionado.[/yellow]")
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
            f"Baixando [bold cyan]{details['title']}[/bold cyan] — "
            f"{len(order)} capítulo(s), formato {args.format.upper()}, modo {mode}"
        )

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Baixando...", total=len(order))
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
    console.print("[green]Concluído.[/green] Conferir:", config.download_dir.resolve())


def _download_suwayomi(args) -> None:
    from .client import SuwayomiClient
    from .downloader import ChapterDownloader

    _apply_delay(args)
    client = SuwayomiClient()
    manga_id = int(args.manga_id[0])  # suwayomi usa id numérico único
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
        console.print("[yellow]Nenhum capítulo selecionado.[/yellow]")
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
        f"Baixando [bold cyan]{manga_meta['title']}[/bold cyan] — "
        f"{len(order)} capítulo(s), formato {args.format.upper()}, modo {mode}"
    )

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
    ) as progress:
        task = progress.add_task("Baixando...", total=len(order))
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
    console.print("[green]Concluído.[/green] Conferir:", config.download_dir.resolve())


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
        description="Manga downloader CLI com extensões Aidoku (WASM) — leve e sem Java.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="logs detalhados")

    sub = parser.add_subparsers(dest="command", required=True)

    # server / extensions (suwayomi legado)
    p_server = sub.add_parser("server", help="[suwayomi] Gerencia o Suwayomi-Server")
    p_server.add_argument("action", choices=["download", "start", "stop", "status"])
    p_server.add_argument("--force", action="store_true")
    p_server.set_defaults(func=cmd_server)

    p_ext = sub.add_parser("extensions", aliases=["ext"], help="[suwayomi] Extensões Mihon")
    ext_sub = p_ext.add_subparsers(dest="ext_action", required=True)
    ext_sub.add_parser("list")
    p_i = ext_sub.add_parser("install"); p_i.add_argument("pkg", nargs="+")
    p_u = ext_sub.add_parser("uninstall"); p_u.add_argument("pkg", nargs="+")
    p_ext.set_defaults(func=cmd_extensions)

    # sources
    p_src = sub.add_parser("sources", help="Gerencia sources Aidoku")
    src_sub = p_src.add_subparsers(dest="source_action")
    src_sub.add_parser("list", help="Lista instalados + disponíveis")
    p_ss = src_sub.add_parser("search", help="Busca no índice")
    p_ss.add_argument("term")
    p_si = src_sub.add_parser("install", help="Instala source(s)")
    p_si.add_argument("source_id", nargs="+")
    p_su = src_sub.add_parser("uninstall", help="Remove source(s)")
    p_su.add_argument("source_id", nargs="+")
    src_sub.add_parser("dir", help="Mostra a pasta de sources")
    p_src.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_src.set_defaults(func=cmd_sources)

    # search
    p_search = sub.add_parser("search", help="Busca mangás")
    p_search.add_argument("query")
    p_search.add_argument("--source", help="ID do source (default: todos instalados)")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_search.set_defaults(func=cmd_search)

    # info
    p_info = sub.add_parser("info", help="Detalhes do mangá")
    p_info.add_argument("manga_id", help="ID retornado pela busca (sourceId::key)")
    p_info.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_info.set_defaults(func=cmd_info)

    # download
    p_dl = sub.add_parser("download", help="Baixa capítulos")
    p_dl.add_argument("manga_id", nargs="+", help="ID(s) da busca (batch: vários IDs)")
    p_dl.add_argument(
        "--format", "-f", choices=["raw", "epub", "cbz", "cb7", "pdf"],
        default=config.default_format,
    )
    p_dl.add_argument(
        "--mode",
        choices=["chapter", "volume", "single"],
        default="chapter",
        help="empacotamento: por capítulo (default), por volume ou tudo num arquivo só",
    )
    p_dl.add_argument("--chapters", "-c", default=None, help='"all", "latest", "1-10,15"')
    p_dl.add_argument("--start-chapter", type=float, default=None)
    p_dl.add_argument("--end-chapter", type=float, default=None)
    p_dl.add_argument(
        "--pages", default=None,
        help='faixa de páginas: "2", "1-10", "3--" (as 3 últimas)',
    )
    p_dl.add_argument("--no-oneshot", action="store_true", help="ignora capítulos oneshot")
    p_dl.add_argument(
        "--order", choices=["desc", "asc"], default="desc",
        help="ordem de download (default: mais recente primeiro)",
    )
    p_dl.add_argument("--cover", action="store_true", help="baixa a capa do mangá")
    p_dl.add_argument("--batch-from-file", default=None, metavar="FILE",
                      help="arquivo com um ID por linha")
    p_dl.add_argument("--workers", type=int, default=4)
    p_dl.add_argument("--retries", type=int, default=3)
    p_dl.add_argument(
        "--delay-requests", type=float, default=None, metavar="SEC",
        help="intervalo mínimo entre requests (rate limit)",
    )
    p_dl.add_argument("--proxy", default=None, help="proxy HTTP/SOCKS (ex.: socks5://localhost:9050)")
    p_dl.add_argument("-o", "--output", default=None)
    p_dl.add_argument("--engine", default="aidoku", choices=["aidoku", "suwayomi"])
    p_dl.set_defaults(func=cmd_download)

    # search ganha --pick (baixa direto dos resultados)
    p_search.add_argument(
        "--pick", action="store_true",
        help="seleciona resultados interativamente e baixa o mais recente de cada",
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
        err_console.print(f"[red]Erro:[/red] {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        err_console.print(f"[red]Erro HTTP {status}:[/red] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        err_console.print("\nInterrompido.")
        sys.exit(130)


if __name__ == "__main__":
    main()
