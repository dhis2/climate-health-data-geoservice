"""Ingest the datasets each instance declares in its datasets.yaml.

    uv run python scripts/ingest.py opendengue/lao            # one instance
    uv run python scripts/ingest.py --all                     # every instance with a datasets.yaml
    uv run python scripts/ingest.py opendengue/lao --only overture_admin1

Open Geo Service reads GEOSERVICE_CONFIG once per process, so each instance is ingested in its own
subprocess with GEOSERVICE_CONFIG pointing at that instance's geoservice.yaml. No server is needed;
start one afterwards to browse the result.

Re-running is safe: a raster whose requested window is already stored does not query the source.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
INSTANCES = ROOT / "instances"


def _instance_dirs(names: list[str], all_: bool) -> list[Path]:
    if all_:
        return sorted(p.parent for p in INSTANCES.glob("*/*/datasets.yaml"))
    dirs = []
    for name in names:
        path = (INSTANCES / name).resolve()
        if not (path / "datasets.yaml").is_file():
            sys.exit(f"No datasets.yaml in {path}")
        dirs.append(path)
    return dirs


def _rasters(spec: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for entry in spec.get("rasters") or []:
        item = {"id": entry} if isinstance(entry, str) else dict(entry)
        item.setdefault("start", spec.get("start"))
        item.setdefault("end", spec.get("end"))
        out.append({k: str(v) for k, v in item.items() if v is not None})
    return out


def run_worker(instance: Path, only: set[str]) -> int:
    """Ingest one instance. Runs in a subprocess with GEOSERVICE_CONFIG already set."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # credentials, shared by every instance

    from open_geo_service.features.services import refresh_feature_collection_from_provider
    from open_geo_service.ingestions.processes import execute_ingestion

    spec = yaml.safe_load((instance / "datasets.yaml").read_text(encoding="utf-8")) or {}
    label = instance.relative_to(INSTANCES).as_posix()
    failures: list[str] = []

    for vector_id in spec.get("vectors") or []:
        if only and vector_id not in only:
            continue
        print(f"[{label}] vector {vector_id}: refreshing", flush=True)
        try:
            record = refresh_feature_collection_from_provider(vector_id, publish=True)
            print(f"[{label}] vector {vector_id}: done ({record})", flush=True)
        except Exception as exc:  # noqa: BLE001 — report and carry on with the next dataset
            logging.exception("vector %s failed", vector_id)
            failures.append(f"{vector_id}: {exc}")

    for raster in _rasters(spec):
        dataset_id = raster["id"]
        if only and dataset_id not in only:
            continue
        print(f"[{label}] raster {dataset_id}: {raster.get('start')} .. {raster.get('end')}", flush=True)
        began = time.monotonic()

        def progress(done: int | None, total: int | None, message: str | None, _id: str = dataset_id) -> None:
            print(f"[{label}] raster {_id}: {done}/{total} {message or ''}".rstrip(), flush=True)

        try:
            result = execute_ingestion(
                dataset_id=dataset_id,
                start=raster.get("start"),
                end=raster.get("end"),
                publish=True,
                on_progress=progress,
            )
            print(f"[{label}] raster {dataset_id}: done in {time.monotonic() - began:.0f}s ({result})", flush=True)
        except Exception as exc:  # noqa: BLE001 — report and carry on with the next dataset
            logging.exception("raster %s failed", dataset_id)
            failures.append(f"{dataset_id}: {exc}")

    if failures:
        print(f"[{label}] FAILED: " + "; ".join(failures), flush=True)
        return 1
    print(f"[{label}] all datasets done", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="*", help="instance paths under instances/, e.g. opendengue/lao")
    parser.add_argument("--all", action="store_true", help="every instance with a datasets.yaml")
    parser.add_argument("--only", action="append", default=[], help="limit to these dataset ids (repeatable)")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.worker:
        return run_worker(Path(args.worker), set(args.only))

    dirs = _instance_dirs(args.instances, args.all)
    if not dirs:
        parser.error("name at least one instance, or pass --all")

    status = 0
    for instance in dirs:
        env = {**os.environ, "GEOSERVICE_CONFIG": str(instance / "geoservice.yaml")}
        cmd = [sys.executable, __file__, "--worker", str(instance)]
        for dataset_id in args.only:
            cmd += ["--only", dataset_id]
        status |= subprocess.call(cmd, env=env, cwd=ROOT)
    return status


if __name__ == "__main__":
    sys.exit(main())
