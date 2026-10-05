import argparse
import os
import random
import re
import shlex
import statistics
import sys
from pathlib import Path

from kb import (
    bundle,
    catalog,
    db,
    domain,
    evaluate,
    extract,
    fetch,
    index,
    keys,
    parse,
    scopes,
    setup,
    shell,
    sources,
    statements,
)

DOWNLOADS = Path("downloads")
INGEST_SCRIPT = "ingest.sh"  # written into the downloads folder, beside the per-source folders


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex: {exc}") from exc
    return value


def main(argv: list[str] | None = None) -> int:
    registry = argparse.ArgumentParser(add_help=False)
    registry.add_argument(
        "--file", type=Path, default=Path("sources.yaml"), help="source registry (default: %(default)s)"
    )

    parser = argparse.ArgumentParser(
        prog="kb",
        description="Cited, versioned knowledge base. Without a command on a terminal, opens an interactive menu "
        "to create, build, register, publish and pull knowledge bases.",
    )
    parser.add_argument(
        "-C",
        dest="home",
        type=Path,
        metavar="DIR",
        help="knowledge base directory; every relative path, defaults included, resolves against it",
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_PATH, help="database file (default: %(default)s)")
    parser.add_argument(
        "--domain", type=Path, default=Path("domain.yaml"), help="domain definition (default: %(default)s)"
    )
    commands = parser.add_subparsers(dest="command")

    cmd = commands.add_parser(
        "sources", parents=[registry], help="validate the domain and source registry and sync them into the database"
    )
    cmd.add_argument("--check", action="store_true", help="validate only; do not touch the database")

    cmd = commands.add_parser(
        "fetch",
        parents=[registry],
        help="download sources; changed content becomes a new version",
        description="Download every source (or those given with --source) into the raw directory. "
        "Content with a new hash is recorded as a new version; old versions are never overwritten. "
        "A file saved by hand into DOWNLOADS/<source id>/ is stored instead of downloading the URL and is then "
        "removed from there; failed sources are listed with their links and folders for that, and "
        "DOWNLOADS/ingest.sh is written to store and ingest them once saved. "
        "Exits 1 if any download failed.",
    )
    cmd.add_argument("--source", action="append", metavar="ID", help="fetch only this source (repeatable)")
    cmd.add_argument("--raw", type=Path, default=Path("raw"), help="download directory (default: %(default)s)")
    cmd.add_argument(
        "--downloads",
        type=Path,
        default=DOWNLOADS,
        help="folder of files saved by hand, one subfolder per source id (default: %(default)s)",
    )

    cmd = commands.add_parser(
        "parse",
        parents=[registry],
        help="split the current version of each source into section chunks",
        description="Extract text from the current version of every source (or those given with --source) and "
        "replace its chunks. sources.yaml can set section_pattern and chapter_pattern (regexes with a "
        "(?P<ref>...) group; HTML headings appear as '## Title', blockquote lines as '> text'), body_start and "
        "body_end (regexes for the first "
        "body line and the first line after the body), section_label, chapter_label and first_chapter (ref "
        "templates and prefixes), and skip_classes (HTML class names whose elements are left out). Without a "
        "pattern, sections split at HTML headings or PDF pages. Prints chunk "
        "counts and sizes per source. Exits 1 if any source failed, or if a version's chunks are already cited "
        "by statements.",
    )
    cmd.add_argument("--source", action="append", metavar="ID", help="parse only this source (repeatable)")
    cmd.add_argument("--raw", type=Path, default=Path("raw"), help="download directory (default: %(default)s)")
    cmd.add_argument("--sample", type=int, default=0, metavar="N", help="print N random chunks per source")

    cmd = commands.add_parser(
        "extract",
        parents=[registry],
        help="extract statements from parsed sections with Claude (pi -p)",
        description="Send every section of the current version of each source (or those given with --source) "
        "to Claude through the local pi login (pi -p, no tools or extensions except pi-anthropic-auth when "
        "installed, which an Anthropic subscription login needs; KB_PI_EXTENSIONS overrides that list, "
        "KB_PI_PREFIX wraps the command, e.g. in a sandbox) and store the statements whose quote appears "
        "verbatim in the section. The system prompt is the --prompt file followed by the modalities and topics "
        "of the domain. Outputs are cached per prompt, model and section, so re-runs only call the model for new "
        "or changed sections. Preamble chunks are skipped. --matching limits extraction to sections whose text "
        "matches a regex, e.g. to tag a new topic without re-running every section; the other sections keep "
        "their earlier statements, even when the prompt has changed since. Exits 1 if any section failed.",
    )
    cmd.add_argument("--source", action="append", metavar="ID", help="extract only this source (repeatable)")
    cmd.add_argument(
        "--prompt", type=Path, default=statements.DEFAULT_PROMPT, help="extraction prompt (default: %(default)s)"
    )
    cmd.add_argument("--model", default=statements.DEFAULT_MODEL, help="pi model (default: %(default)s)")
    cmd.add_argument("--workers", type=int, default=4, help="parallel model calls (default: %(default)s)")
    cmd.add_argument("--matching", metavar="REGEX", type=_regex, help="extract only sections whose text matches")

    cmd = commands.add_parser(
        "links",
        parents=[registry],
        help="list the links in a source's current version",
        description="Print, one per line, the absolute http(s) URLs the current version of a source links to, in "
        "document order and each once: <a href> in the HTML text the parser keeps (no navigation or footers) "
        "and URI link annotations in a PDF. Fragments are dropped, links into the source itself left out. Add the "
        "ones worth keeping to sources.yaml. Exits 1 when the source is unknown or has no fetched version.",
    )
    cmd.add_argument("id", help="source id")
    cmd.add_argument("--raw", type=Path, default=Path("raw"), help="download directory (default: %(default)s)")

    commands.add_parser(
        "index",
        help="rebuild the full-text index and embed new or changed sections and statements",
        description="Rebuild the FTS5 tables from the current versions and embed, with the local "
        f"{index.EMBED_MODEL} model, every section and statement whose text is new or changed; vectors of "
        "items that no longer exist are dropped. The first run downloads the model (about 2 GB).",
    )
    commands.add_parser(
        "serve",
        help="run the read-only MCP server on stdio",
        description="Serve kb_search, kb_get, kb_topic and kb_sources over MCP on stdin/stdout, reading the "
        "database read-only. The embedding model is loaded offline on the first search. Without the domain file "
        "the copy of it stored in a published database is used.",
    )
    commands.add_parser(
        "keygen",
        help="create the age identity that opens published knowledge bases",
        description=f"Write a new age identity to ${keys.IDENTITY_ENV} (default: {keys.DEFAULT_IDENTITY}), "
        "readable only by you, and print its public key: give that to whoever publishes a knowledge base for you. "
        "An existing identity is never replaced; its public key is printed instead.",
    )
    cmd = commands.add_parser(
        "unpack",
        help="write the configuration files stored in a published database",
        description="Write domain.yaml, sources.yaml, prompts/ and eval/ as stored in the database by kb publish. "
        "Files that exist with other content are listed and kept unless --force.",
    )
    cmd.add_argument("--force", action="store_true", help="overwrite files that differ")
    cmd = commands.add_parser(
        "eval",
        help="measure search quality against the golden questions",
        description="Run every question in the golden file through kb_search and report whether an expected "
        "section is in the top K, and re-check that every stored quote still appears in its section. Exits 1 "
        "when the hit rate is below --min or a quote fails.",
    )
    cmd.add_argument("--file", type=Path, default=Path("eval/golden.yaml"), help="golden set (default: %(default)s)")
    cmd.add_argument("--k", type=int, default=5, help="rank cut-off (default: %(default)s)")
    cmd.add_argument("--min", type=float, default=0.9, help="required hit rate (default: %(default)s)")
    cmd = commands.add_parser(
        "setup",
        help="create a knowledge base directory, or build and register an existing one",
        description="Without a domain.yaml in the directory, ask what the knowledge base is about and write the "
        "template files (domain.yaml, sources.yaml, prompts/extract.md, eval/golden.yaml) plus a .gitignore. "
        "With one, run sources --check, fetch, parse, extract and index, offer to register the MCP server under "
        "NAME in omp's mcp.json, and run eval once the golden set has questions. Safe to re-run: unchanged "
        "downloads, sections and model outputs are reused. Exits 1 if any step failed.",
    )
    cmd.add_argument("name", help="knowledge base name, e.g. running; also its MCP server name")
    cmd.add_argument("--dir", type=Path, help="knowledge base directory (default: ~/kbs/NAME)")
    cmd.add_argument(
        "--omp-config", type=Path, default=setup.OMP_MCP, help="omp MCP config to register in (default: %(default)s)"
    )
    shelf = os.environ.get("KB_CATALOG") or None  # empty would mean the current directory
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--catalog",
        type=Path,
        default=shelf,
        required=shelf is None,
        help="catalog directory, e.g. a git clone or a synced cloud folder (default: $KB_CATALOG)",
    )
    cmd = commands.add_parser(
        "publish",
        parents=[shared],
        help="publish this knowledge base, encrypted, to a catalog",
        description="Snapshot the database without the derived search indexes (kb pull rebuilds them), with the "
        "configuration files inside, compress it and encrypt it with age to every key in "
        "CATALOG/NAME/recipients.txt, your own included. A folder catalog keeps the bundle in CATALOG/NAME; a git "
        "clone is pulled first, the bundle uploaded as a GitHub release asset with gh, and the manifest committed "
        "and pushed. A public knowledge base also gets a readable copy of its configuration in CATALOG/NAME. Prints "
        "unchanged and publishes nothing when content, recipients and privacy are the same as the last version. "
        "Exits 1 when the knowledge base is not built, a key is missing or invalid, or the upload or push failed.",
    )
    cmd.add_argument("--name", help="name in the catalog (default: the directory name)")
    cmd.add_argument(
        "--recipient", action="append", default=[], metavar="AGE1...", help="add a public key (repeatable)"
    )
    cmd.add_argument(
        "--private",
        action=argparse.BooleanOptionalAction,
        help="keep the configuration out of the catalog too, only inside the bundle (default: as last published)",
    )
    commands.add_parser(
        "catalog",
        parents=[shared],
        help="list the knowledge bases in a catalog",
        description="Print every knowledge base in the catalog with its version, bundle size, the version ~/kbs "
        "holds, and its name from domain.yaml ((private) for a private one). A git catalog is pulled first.",
    )
    cmd = commands.add_parser(
        "pull",
        parents=[shared],
        help="install a knowledge base from a catalog and register it",
        description="Download and decrypt CATALOG/NAME with your age identity, write its database and "
        "configuration into ~/kbs/NAME, rebuild the search index (downloading the embedding model when it is not "
        "cached) and offer to register NAME as an MCP server in omp's mcp.json. An existing knowledge base is "
        "replaced only with --force: its vectors are reused for unchanged text, raw/ and other local files kept. A "
        "git catalog is pulled first.",
    )
    cmd.add_argument("name", help="knowledge base name in the catalog")
    cmd.add_argument("--dir", type=Path, help="target directory (default: ~/kbs/NAME)")
    cmd.add_argument("--force", action="store_true", help="replace an existing knowledge base")
    cmd.add_argument(
        "--omp-config", type=Path, default=setup.OMP_MCP, help="omp MCP config to register in (default: %(default)s)"
    )
    args = parser.parse_args(argv)
    if args.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return shell.run(main, args.home)  # before the chdir below, so a relative -C resolves here
        parser.print_usage(sys.stderr)
        print("kb: a command is required without a terminal", file=sys.stderr)
        return 2
    if args.home is not None:
        try:
            os.chdir(args.home)
        except OSError as exc:
            print(f"-C {args.home}: {exc.strerror}", file=sys.stderr)
            return 1
    if args.command == "setup":
        return setup.setup(args.name, args.dir, input, main, args.omp_config)
    if args.command == "keygen":
        return run_keygen()
    if args.command == "publish":
        return catalog.publish(Path.cwd(), args.catalog, args.name or Path.cwd().name, args.recipient, args.private)
    if args.command == "catalog":
        return catalog.show(args.catalog)
    if args.command == "pull":
        return catalog.pull(args.name, args.catalog, args.dir, args.force, input, run_index, args.omp_config)
    if args.command == "unpack":
        return run_unpack(args.db, args.force)
    if args.command == "index":
        return run_index(args.db)
    if args.command == "eval":
        try:
            evaluated = serving_domain(args.domain, args.db)
        except domain.ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        return evaluate.run(args.db, evaluated, args.file, args.k, args.min)

    try:
        if args.command == "serve":
            from kb import server  # the MCP SDK is only needed here

            server.serve(args.db, serving_domain(args.domain, args.db))
            return 0
        defined = domain.load(args.domain)
        found = sources.load(args.file, defined)
        registry = scopes.load(scopes.PATH, defined, found) if defined.scope_label else []
        system = statements.system_prompt(args.prompt, defined) if args.command == "extract" else None
    except domain.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.command == "sources" and args.check:
        listed = f", {len(registry)} {defined.scope_label} entries" if defined.scope_label else ""
        print(f"domain{listed} and {len(found)} sources valid")
        return 0
    if args.command == "links":
        return run_links(found, args.id, args.db, args.raw)

    if args.command in {"fetch", "parse", "extract"} and args.source:
        unknown = sorted(set(args.source) - {s.id for s in found})
        if unknown:
            print(f"unknown source ids: {', '.join(unknown)}", file=sys.stderr)
            return 1
        selected = [s for s in found if s.id in args.source]
    else:
        selected = found

    conn = db.connect(args.db)
    try:
        stale = sources.sync(conn, found)
        statements.sync_topics(conn, defined.topics)
        scopes.sync(conn, registry)
        results = fetch.fetch_all(conn, selected, args.raw, inbox=args.downloads) if args.command == "fetch" else None
        parsed = parse.parse_all(conn, selected, args.raw) if args.command == "parse" else None
        extracted = None
        if system is not None:
            extracted = statements.extract_all(
                conn,
                selected,
                defined,
                system,
                args.model,
                args.workers,
                progress=lambda line: print(line, file=sys.stderr),
                matching=args.matching,
            )
    finally:
        conn.close()

    for doc_id in stale:
        print(f"warning: {doc_id} is in the database but no longer in {args.file}", file=sys.stderr)
    if parsed is not None:
        return report_parse(parsed, args.sample)
    if extracted is not None:
        return report_extract(extracted)
    if results is None:
        print(f"{len(found)} sources and {len(defined.topics)} topics synced to {args.db}")
        return 0
    for result in results:
        print(f"{result.status:<9} {result.source_id}  {result.detail}")
    counts = {status: sum(r.status == status for r in results) for status in ("new", "changed", "unchanged", "failed")}
    print(", ".join(f"{n} {status}" for status, n in counts.items()))
    failed = [r.source_id for r in results if r.status == "failed"]
    if failed:
        report_manual([s for s in selected if s.id in failed], args)
    return 1 if failed else 0


def report_manual(failed: list[sources.Source], args: argparse.Namespace) -> None:
    """List the sources to download in a browser with the folder to save each into (created here), and write
    the script that stores and ingests them, with the options of this run that differ from the defaults."""
    downloads = args.downloads
    for source in failed:
        (downloads / source.id).mkdir(parents=True, exist_ok=True)
    folders = [f"{downloads.resolve() / s.id}/" for s in failed]
    width = max(len("save into"), *map(len, folders))
    table = [f"{'#':>3}  {'save into':<{width}}  link"]
    table += [
        f"{n:>3}  {folder:<{width}}  {s.url}" for n, (s, folder) in enumerate(zip(failed, folders, strict=True), 1)
    ]
    print(f"\nDownload {len(failed)} by hand: open each link in a browser and save the PDF, or the page as HTML,")
    print("into the folder on its line (one file per folder; the file name does not matter):\n")
    print("\n".join(f"  {line}" for line in table))
    script = downloads.resolve() / INGEST_SCRIPT
    fetch.write_atomic(script, ingest_script(failed, args, table).encode())  # atomic: the script may be running
    script.chmod(0o755)
    print("\nThen run this script; it stores the saved files (removing them from their folders) and ingests them:")
    print(f"\n  {shlex.quote(str(script))}\n")
    print("A source still without a file is tried online again; if that fails, it is listed again and the")
    print("script rewritten. The script removes itself once every step succeeded.")


def ingest_script(failed: list[sources.Source], args: argparse.Namespace, table: list[str]) -> str:
    """A POSIX shell script running fetch, parse and extract for the failed sources, then index; every step runs
    even when an earlier one failed, and the script exits 1 if any did."""
    kb = [
        "uv",
        "run",
        "--project",
        shlex.quote(str(setup.ENGINE)),
        "--quiet",
        "kb",
        "-C",
        shlex.quote(str(Path.cwd())),
        *_changed("--db", args.db, db.DEFAULT_PATH),
        *_changed("--domain", args.domain, Path("domain.yaml")),
    ]
    registry = _changed("--file", args.file, Path("sources.yaml"))
    raw = _changed("--raw", args.raw, Path("raw"))
    only = [f"--source {s.id}" for s in failed]
    steps = [
        ["fetch", *registry, *raw, *_changed("--downloads", args.downloads, DOWNLOADS), *only],
        ["parse", *registry, *raw, *only],
        ["extract", *registry, *only],
        ["index"],
    ]
    return "\n".join(
        [
            "#!/bin/sh",
            "# Written by `kb fetch` for the sources it could not download. Save each file into the folder",
            "# on its line, then run this script to store and ingest them:",
            "#",
            *(f"# {line}" for line in table),
            "",
            f'kb() {{ {" ".join(kb)} "$@"; }}',
            "status=0",
            *(f"kb {' '.join(step)} || status=1" for step in steps),
            'if [ "$status" -eq 0 ]; then rm -f -- "$0"; fi',
            'exit "$status"',
            "",
        ]
    )


def _changed(option: str, value: Path, default: Path) -> list[str]:
    """[option, absolute value] when value is not the default, else nothing."""
    return [] if value == default else [option, shlex.quote(str(value.resolve()))]


def report_parse(parsed: list[parse.Parsed], sample: int) -> int:
    print(f"{'source':<34} {'chunks':>6} {'min':>6} {'median':>6} {'max':>6}  first refs")
    for result in parsed:
        if result.error:
            print(f"{result.source_id:<34} failed: {result.error}")
            continue
        if result.kept:
            print(f"{result.source_id:<34} kept: download not on this device, stored sections unchanged")
            continue
        sizes = [len(s.text) for s in result.sections]
        refs = ", ".join(s.ref for s in result.sections[:4])
        print(
            f"{result.source_id:<34} {len(sizes):>6} {min(sizes):>6} {int(statistics.median(sizes)):>6} "
            f"{max(sizes):>6}  {refs[:60]}"
        )
        for section in random.sample(result.sections, min(sample, len(result.sections))):
            print(f"  --- {section.ref}  [{' > '.join(section.heading_path)[:90]}]")
            print("  " + section.text[:600].replace("\n", "\n  "))
    failed = sum(1 for r in parsed if r.error)
    kept = sum(1 for r in parsed if r.kept)
    print(f"{len(parsed) - failed - kept} parsed, " + (f"{kept} kept, " if kept else "") + f"{failed} failed")
    return 1 if failed else 0


def report_extract(reports: list[statements.Report]) -> int:
    print(f"{'source':<34} {'sections':>8} {'cached':>6} {'called':>6} {'stmts':>5} {'rejected':>8} {'failed':>6}")
    for r in reports:
        print(
            f"{r.source_id:<34} {r.sections:>8} {r.cached:>6} {r.called:>6} {r.statements:>5} "
            f"{len(r.rejected):>8} {len(r.failed):>6}"
        )
        if r.skipped:
            print(f"  {r.skipped} sections skipped (skip_sections)")
        for line in [*r.failed[:5], *r.rejected[:5]]:
            print(f"  {line}")
    failed = sum(len(r.failed) for r in reports)
    print(
        f"{sum(r.statements for r in reports)} statements, {sum(len(r.rejected) for r in reports)} rejected, "
        f"{failed} sections failed"
    )
    return 1 if failed else 0


def run_links(found: list[sources.Source], source_id: str, db_path: Path, raw: Path) -> int:
    source = next((s for s in found if s.id == source_id), None)
    if source is None:
        print(f"unknown source id: {source_id}", file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    try:
        version = parse.current_version(conn, source.id)
    finally:
        conn.close()
    if version is None:
        print(f"{source.id} has no fetched version; run kb fetch first", file=sys.stderr)
        return 1
    try:
        urls = extract.links(raw / version[1], version[2], source.url, source.skip_classes)
    except (OSError, ValueError) as exc:
        print(f"{source.id}: {exc}", file=sys.stderr)
        return 1
    for url in urls:
        print(url)
    return 0


def run_index(path: Path) -> int:
    if not path.exists():
        print(f"{path} does not exist; run sources, fetch, parse and extract first", file=sys.stderr)
        return 1
    conn = db.connect(path)
    try:
        chunks, stmts = index.build_fts(conn)
        print(f"full-text index: {chunks} sections, {stmts} statements")
        embed = None
        for kind, texts in (("chunk", index.chunk_texts(conn)), ("statement", index.statement_texts(conn))):
            if embed is None and texts:
                embed = index.load_model()
            if embed is not None:
                done, removed = index.sync_vectors(
                    conn, kind, texts, index.EMBED_MODEL, embed, progress=lambda line: print(line, file=sys.stderr)
                )
                print(f"{kind} vectors: {done} embedded, {removed} removed, {len(texts)} current")
    finally:
        conn.close()
    return 0


def run_keygen() -> int:
    path = keys.identity_path()
    if path.exists():
        try:
            public = keys.public_keys(keys.load(path))
        except keys.KeysError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{path} exists; its public key{'s' if len(public) > 1 else ''}:")
        print("\n".join(public))
        return 0
    public = keys.generate(path)
    print(f"wrote {path}; back it up, it is the only way to open what is published for it. Public key:")
    print(public)
    return 0


def run_unpack(db_path: Path, force: bool) -> int:
    files = bundle.stored_files(db_path) if db_path.exists() else {}
    if not files:
        print(f"{db_path} holds no configuration files; only a database from kb publish does", file=sys.stderr)
        return 1
    try:
        written, differing = bundle.write_files(files, Path.cwd(), overwrite=force)
    except bundle.BundleError as exc:
        print(exc, file=sys.stderr)
        return 1
    for rel in written:
        print(f"wrote {rel}")
    for rel in differing:
        print(f"kept {rel}: it differs from the stored copy (--force overwrites)")
    print(f"{len(written)} written, {len(files) - len(written) - len(differing)} unchanged, {len(differing)} kept")
    return 0


def serving_domain(path: Path, db_path: Path) -> domain.Domain:
    """domain.yaml when it exists, else the copy a published database stores."""
    if path.exists() or not db_path.exists():
        return domain.load(path)
    text = bundle.stored_files(db_path).get("domain.yaml")
    if text is None:
        return domain.load(path)  # raises the missing-file error
    return domain.parse(text, f"{db_path}:domain.yaml")
