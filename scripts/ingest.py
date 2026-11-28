"""Ingest the datasets each instance declares in its datasets.yaml.

    uv run python scripts/ingest.py lao                       # one instance
    uv run python scripts/ingest.py --all                     # every instance with a datasets.yaml
    uv run python scripts/ingest.py lao --only natural_earth_admin1

Open Geo Service reads GEOSERVICE_CONFIG once per process, so each instance is ingested in its own
subprocess with GEOSERVICE_CONFIG pointing at that instance's geoservice.yaml. No server is needed;
start one afterwards to browse the result.

Order: vectors, then tables (their rows are checked against a vector collection), then rasters,
then derived datasets (made from rasters by a recipe in plugins/rasters/; created here the first
time, then kept current by the server's recipe reconciliation).

Re-running is safe: a raster whose requested window is already stored does not query the source,
and a table's fetched periods replace their stored rows.
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
        return sorted(p.parent for p in INSTANCES.glob("*/datasets.yaml"))
    dirs = []
    for name in names:
        path = (INSTANCES / name).resolve()
        if not (path / "datasets.yaml").is_file():
            sys.exit(f"No datasets.yaml in {path}")
        dirs.append(path)
    return dirs


def _entries(spec: dict[str, Any], kind: str, *, default_window: bool) -> list[dict[str, str]]:
    """The `kind` entries of a datasets.yaml, each with its id and window.

    Rasters take the file's `start`/`end` unless an entry overrides them. Tables take only their
    own: without one, the whole period the source has.
    """
    out = []
    for entry in spec.get(kind) or []:
        item = {"id": entry} if isinstance(entry, str) else dict(entry)
        if default_window:
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

    # Tables after vectors, since a table's rows are checked against its feature collection.
    work = [("table", t) for t in _entries(spec, "tables", default_window=False)]
    work += [("raster", r) for r in _entries(spec, "rasters", default_window=True)]
    for kind, item in work:
        dataset_id = item["id"]
        if only and dataset_id not in only:
            continue
        print(f"[{label}] {kind} {dataset_id}: {item.get('start', 'first')} .. {item.get('end', 'last')}", flush=True)
        began = time.monotonic()

        def progress(
            done: int | None, total: int | None, message: str | None, _id: str = dataset_id, _kind: str = kind
        ) -> None:
            print(f"[{label}] {_kind} {_id}: {done}/{total} {message or ''}".rstrip(), flush=True)

        try:
            result = execute_ingestion(
                dataset_id=dataset_id,
                start=item.get("start"),
                end=item.get("end"),
                publish=True,
                on_progress=progress,
            )
            print(f"[{label}] {kind} {dataset_id}: done in {time.monotonic() - began:.0f}s ({result})", flush=True)
        except Exception as exc:  # noqa: BLE001 — report and carry on with the next dataset
            logging.exception("%s %s failed", kind, dataset_id)
            failures.append(f"{dataset_id}: {getattr(exc, 'detail', None) or exc}")

    for dataset_id in spec.get("derived") or []:
        if only and dataset_id not in only:
            continue
        began = time.monotonic()
        try:
            created = _create_derived(dataset_id)
            state = f"created in {time.monotonic() - began:.0f}s" if created else "exists; its recipe keeps it current"
            print(f"[{label}] derived {dataset_id}: {state}", flush=True)
        except Exception as exc:  # noqa: BLE001 — report and carry on with the next dataset
            logging.exception("derived %s failed", dataset_id)
            failures.append(f"{dataset_id}: {getattr(exc, 'detail', None) or exc}")

    if failures:
        print(f"[{label}] FAILED: " + "; ".join(failures), flush=True)
        return 1
    print(f"[{label}] all datasets done", flush=True)
    return 0


def _create_derived(dataset_id: str) -> bool:
    """Create a derived dataset by running its recipe over its input's whole coverage; False if it exists.

    The same call Open Geo Service makes when it refreshes the dataset after an update: a batch
    job running the recipe's workflow, which is what publishes a `save_result` dataset (running the
    graph alone only returns the result). Once the dataset exists, a server's recipe
    reconciliation (at startup and every 15 minutes) refreshes it after each update of its input.
    """
    from open_geo_service.data_registry.recipes import OUTPUT_PARAMETER
    from open_geo_service.derivations.service import list_recipes
    from open_geo_service.ingestions import services as ingestion_services
    from open_geo_service.openeo import workflows as workflow_registry
    from open_geo_service.openeo.jobs import get_openeo_job_service
    from open_geo_service.openeo.schemas import OpenEOJobCreate
    from open_geo_service.pipelines import workflow as binding

    recipe = next((r for r in list_recipes() if r.output == dataset_id), None)
    if recipe is None or recipe.input is None:
        raise ValueError(f"'{dataset_id}' has no recipe with refresh_on; declare one in plugins/rasters/")
    artifacts = ingestion_services.list_artifacts().items
    if any(a.dataset_id == dataset_id for a in artifacts):
        return False
    inputs = [a for a in artifacts if a.dataset_id == recipe.input and a.coverage and a.coverage.temporal]
    if not inputs:
        raise ValueError(f"input '{recipe.input}' of '{dataset_id}' is not ingested; list it under rasters: first")
    coverage = max(inputs, key=lambda a: a.created_at).coverage.temporal

    record = workflow_registry.get_workflow(recipe.workflow)
    if record is None:
        raise ValueError(f"workflow '{recipe.workflow}' is not registered")
    [interval] = [name for name in binding.interval_parameters(binding.frozen(record)) if name not in recipe.arguments]
    arguments = {**recipe.arguments, OUTPUT_PARAMETER: dataset_id, interval: [coverage.start, coverage.end]}
    call = {"derive": {"process_id": recipe.workflow, "arguments": arguments, "result": True}}
    service = get_openeo_job_service()
    job = service.create_job(
        OpenEOJobCreate(
            title=f"Create {dataset_id}",
            description=f"Creates derived dataset '{dataset_id}' with workflow '{recipe.workflow}' over "
            f"'{recipe.input}' {coverage.start}..{coverage.end} (scripts/ingest.py)",
            process={"process_graph": call},
        )
    )
    service.start_job(job.id)
    while (status := service.get_job_or_404(job.id).status.value) not in ("finished", "error", "canceled"):
        time.sleep(3)
    if status != "finished":
        raise RuntimeError(f"job {job.id} ended {status}: {service.get_logs(job.id).logs[-5:]}")
    if not any(a.dataset_id == dataset_id for a in ingestion_services.list_artifacts().items):
        raise RuntimeError(f"job {job.id} finished but '{dataset_id}' was not stored")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="*", help="instance paths under instances/, e.g. lao")
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
