"""Move tracks from a folder into the Lidarr-style library layout (same logic as the web import).

Run inside the container (dry run by default):
    python -m app.tools.reorganize_library /music/SomeFolder
    python -m app.tools.reorganize_library /music/SomeFolder --apply

Every applied import is recorded in /app/data/import-<timestamp>.json.
"""

import argparse
import asyncio
import sys
import uuid

from app.services.library_import import ImportJob, LibraryImporter, resolve_source


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="Folder with tracks to import, inside the music root or the import mount")
    parser.add_argument("--apply", action="store_true", help="Move files (default is a dry run)")
    args = parser.parse_args()

    try:
        source = resolve_source(args.source)
    except ValueError as exc:
        sys.exit(f"{args.source}: {exc}")

    job = ImportJob(id=uuid.uuid4().hex, source=source, label=source)
    await LibraryImporter.plan(job, log=True)
    print(f"\nSummary: {job.summary()}, without online match: {sum(not e['matched'] for e in job.entries)}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to move the files.")
        return
    await LibraryImporter.apply(job)
    print(f"Moved {job.moved} tracks. Manifest: {job.manifest}")


if __name__ == "__main__":
    asyncio.run(main())
