# wotoha

CLI de manga downloader com **extensões Aidoku (WebAssembly)** — leve, em Python puro, **sem Java**.

```
┌──────────┐  executa  ┌────────────────────┐  raspa  ┌──────────────┐
│  wotoha  │ ────────► │  extensão .aix     │ ──────► │  sites de    │
│ (Python) │  wasmtime │  (WASM sandboxed)  │         │  mangá       │
└──────────┘           └────────────────────┘         └──────────────┘
```

- **~80 MB de RAM** no total (o caminho Suwayomi/JVM usa ~2.6 GB)
- Sem JVM, sem Android, sem CEF/Chromium — só Python + wasmtime
- **143+ sources**: índice oficial Aidoku + repositórios de terceiros (Yomu, etc.)
- Download em `raw`, `epub`, `cbz`, `cb7` e `pdf`
- Filtro de **idioma** por comando (`--language`)
- Suporte **opcional** ao Suwayomi-Server (extensões Mihon/keiyoushi) via `--engine suwayomi`

> Inspirado no [rakuyomi](https://github.com/tachibana-shin/rakuyomi) (reader para KOReader
> que roda sources Aidoku/LNReader/Keiyoushi com runtimes embutidos) e no
> [mangadex-downloader](https://github.com/mansuf/mangadex-downloader) (UX de CLI).
> A referência completa está na pasta `REF/`.

## Instalação

```bash
pip install -e .
```

Dependências: `wasmtime`, `beautifulsoup4`, `requests`, `rich`, `Pillow`.

## Uso

```bash
# Sources (extensões Aidoku)
wotoha sources list                    # instalados + disponíveis (agrega todos os repos)
wotoha sources search asura            # buscar em todos os índices
wotoha sources install en.asurascans   # procura o source em todos os repos
wotoha sources uninstall en.asurascans

# Repositórios de terceiros
wotoha sources repo list               # repos cadastrados (aidoku vem de fábrica)
wotoha sources repo add yomu https://smexhy.github.io/yomu-aidoku-sources/index.min.json
wotoha sources install --index yomu en.atsumaru    # ou só 'install en.atsumaru'
wotoha sources install --index https://.../index.min.json multi.kagane  # URL direta

# Descoberta
wotoha search "solo leveling" --source en.asurascans
wotoha search "berserk"                # busca em todos os sources instalados
wotoha info en.asurascans::solo-leveling   # link, tags, idiomas, data do último cap

# Idioma (sources multi-idioma: MangaDex, MangaPlus, Mangafire...)
wotoha search "onimai" -l pt-BR
wotoha info multi.mangadex::<uuid> -l pt-BR        # capítulos traduzidos
wotoha download multi.mangadex::<uuid> -l pt-BR -c 1-10

# Download
wotoha download en.asurascans::solo-leveling -f epub              # tudo
wotoha download en.asurascans::solo-leveling -f cbz -c latest     # último cap
wotoha download en.asurascans::solo-leveling -f pdf -c 1-10,15    # faixas
wotoha download ... -o ./minha-pasta

# Seleção fina
wotoha download ID --pages 1-10        # só páginas 1-10 ("3--" = 3 últimas; "-5" = menos 5)
wotoha download ID --start-chapter 10 --end-chapter 20
wotoha download ID --mode volume -f epub   # empacota por volume
wotoha download ID --mode single -f cbz    # mangá inteiro num arquivo só
wotoha download ID --no-oneshot --order asc --cover
wotoha search "solo" --source en.asurascans --pick  # escolhe na hora e baixa

# Batch
wotoha download ID1 ID2 ID3 -f epub
wotoha download --batch-from-file lista.txt -f cbz

# Rede
wotoha download ID --proxy socks5://localhost:9050
wotoha download ID --delay-requests 2.5   # rate limit (1 req a cada 2.5s)
```

Os IDs de mangá são compostos (`sourceId::key`) e vêm no resultado da busca.

## Repositórios de sources

Além do índice oficial da comunidade Aidoku, o wotoha aceita qualquer repo
que publique um `index.min.json` no mesmo formato (ex.:
[yomu-aidoku-sources](https://github.com/Smexhy/yomu-aidoku-sources)).

```bash
wotoha sources repo add <nome> <url-do-index.min.json>
wotoha sources repo list                 # aidoku (built-in) + os que você adicionar
wotoha sources repo remove <nome>
```

- `list`/`search` agregam **todos** os repos e mostram a coluna **Repo**
  (`aidoku+yomu` quando o source existe em mais de um)
- `install` procura o source em todos os repos; `--index <nome|url>` força um
- A proveniência fica em `~/.wotoha/repos.json` (repos) e
  `~/.wotoha/source_repos.json` (origem de cada source instalado)
- Sources do SDK 0.8 são suportados (Atsumaru, WEBTOON, Mangaball, Kagane...)

## Formatos

| Formato | Saída |
|---|---|
| `raw` | `Titulo/Cap/001.webp ...` |
| `epub` | um `.epub` por capítulo |
| `cbz` | um `.cbz` por capítulo |
| `cb7` | um `.cb7` (7-zip) por capítulo — requer `pip install wotoha[cb7]` |
| `pdf` | um `.pdf` por capítulo |

Todos os formatos empacotados incluem **ComicInfo.xml** (título, número,
volume, autor, tags, data) para Kavita/Komga/Tachiyomi reconhecerem os
metadados — no modo `volume`/`single`, com os metadados consolidados.

## Arquitetura

```
wotoha/
├── aidoku_runtime.py   # host WASM: ABI Aidoku next-SDK (net/html/std/defaults)
├── aidoku_store.py     # índice + instalação de .aix
├── aidoku_catalog.py   # fachada search/manga/chapters/pages
├── native_downloader.py# download de capítulos (resume, retries, paralelo)
├── postcard.py         # codec postcard (formato de serialização do ABI)
├── models.py           # Manga/Chapter/Page com codecs binários
├── formats.py          # raw/epub/cbz/cb7/pdf + modos chapter/volume/single
├── comicinfo.py        # ComicInfo.xml (metadados ComicRack)
├── net.py              # proxy, rate limit, timeout compartilhados
├── batch.py            # seleção interativa (--pick) e parsing de batch
├── downloader.py       # (legado suwayomi)
├── client.py           # (legado suwayomi)
└── server_manager.py   # (legado suwayomi)
```

O runtime implementa o mesmo ABI que o rakuyomi (Rust/wasmi) e o app Aidoku
usam: o source WASM chama imports host (`net.send`, `html.select`,
`std.read_buffer`...) e devolve structs serializadas em postcard, que o
wotoha decodifica. Tudo isolado: a extensão não tem acesso a disco/rede
exceto pelas funções host.

## Suwayomi (opcional, extensões Mihon/keiyoushi)

Se você também quiser os 1380 sources keiyoushi (exigem JVM):

```bash
wotoha server download && wotoha server start
wotoha extensions install eu.kanade.tachiyomi.extension.en.weebcentral
wotoha search "one piece" --engine suwayomi --source 2131019126180322627
wotoha download <id> --engine suwayomi -f epub
```

## Roadmap

- [x] Runtime Aidoku WASM (next-SDK 0.7 **e 0.8**)
- [x] CLI completo (sources/search/info/download/convert)
- [x] ComicInfo.xml, cb7, modos chapter/volume/single, faixa de páginas
- [x] Proxy, rate limit, batch, --pick interativo, --cover, --no-oneshot
- [x] Repos de terceiros (`sources repo`, `--index`, auto-discovery no install)
- [x] Filtro de idioma (`--language` em search/info/download) e info enriquecido
- [ ] Runtime JS — LNReader/Mangayomi via quickjs
- [ ] Filtros avançados de busca (`filters.json`)
- [ ] Comparação de resultados entre sources (mesmo título em vários sources)

## Notas legais

Respeite os termos de uso dos sites e o copyright das obras. Baixe apenas
conteúdo ao qual você tem direito de acesso.
