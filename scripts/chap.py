"""Build each instance's Chap CSV from the `chap:` block of its datasets.yaml.

    uv run python scripts/chap.py lao                                  # one instance, into instances/lao/exports/
    uv run python scripts/chap.py --all                                # every instance with a chap: block
    uv run python scripts/chap.py lao --url http://127.0.0.1:8011      # use a running server
    uv run python scripts/chap.py lao --out ../chap-exports            # into ../chap-exports/lao/ instead

The CSV comes from one openEO process graph, generated from the `chap:` block and run as a batch
job on the instance's own server: the boundaries are loaded once, each column's dataset is loaded
over the file's start..end and aggregated over every boundary unit, a table column is laid on the
same units and periods (load_table), the columns are merged, and core's CHAPCSV writer saves them.

Each column's dataset must be at the `chap.period`, or hold a single static period (population's
year), which is copied to every period. A finer dataset is refused: declare a derived dataset for it
(plugins/rasters/derived.yaml) and list it under `derived:`, so the server keeps it current.

Without --url, a server is started for the instance on a free port and stopped afterwards, so no
other writable server may run on it. Outputs go to the instance's exports/ folder (gitignored), or
<out>/<instance>/ with --out: the CSV, the graph that made
it, the boundaries as GeoJSON, and provenance.json (repo and Open Geo Service commits, the instance
config, and each input as the server reported it).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
INSTANCES = ROOT / "instances"

# The period a dataset's `period_type` names, and how they order from fine to coarse.
PERIOD_OF = {"daily": "day", "weekly": "week", "dekadal": "dekad", "monthly": "month", "yearly": "year"}
ORDER = ["day", "week", "dekad", "month", "year"]
ADJECTIVE = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}


def build_graph(chap: dict[str, Any], start: str, end: str, datasets: dict[str, dict[str, Any]]) -> tuple[dict, dict]:
    """The process graph for one Chap CSV, and the save options it ends with.

    *datasets* is the server's /datasets listing by id, which says each input's period type,
    variable and coverage.
    """
    period = chap["period"]
    if period not in ADJECTIVE:
        raise ValueError(f"chap.period must be one of {list(ADJECTIVE)}, got {period!r}")
    graph: dict[str, Any] = {"bounds": {"process_id": "load_features", "arguments": {"id": chap["boundaries"]}}}
    labels: dict[str, str] = {}
    nodes: list[str] = []
    like: str | None = None
    static: list[tuple[str, str]] = []
    tables: list[tuple[str, dict[str, Any]]] = []

    for column, spec in chap["columns"].items():
        if "table" in spec:
            tables.append((column, spec))
            continue
        dataset_id = spec["dataset"]
        info = datasets.get(dataset_id)
        if info is None:
            raise ValueError(f"column {column}: dataset '{dataset_id}' is not published on this instance")
        own = PERIOD_OF.get(str(info.get("period_type")))
        temporal = (info.get("extent") or {}).get("temporal") or {}
        single = temporal.get("start") is not None and temporal.get("start") == temporal.get("end")
        if own is None:
            raise ValueError(f"column {column}: '{dataset_id}' has no known period type ({info.get('period_type')})")
        if ORDER.index(own) < ORDER.index(period):
            raise ValueError(
                f"column {column}: '{dataset_id}' is {info['period_type']}, finer than {period}; "
                "declare a derived dataset for it (plugins/rasters/derived.yaml) and use that"
            )
        if own != period and not single:
            raise ValueError(
                f"column {column}: '{dataset_id}' is {info['period_type']} with several periods; only a single "
                f"static period can be copied to every {period}"
            )
        node = f"{column}_load"
        arguments: dict[str, Any] = {"id": dataset_id}
        if own == period:
            arguments["temporal_extent"] = [start, end]
        graph[node] = {"process_id": "load_collection", "arguments": arguments}
        graph[f"{column}_agg"] = {
            "process_id": "aggregate_spatial_weighted",
            "arguments": {"data": {"from_node": node}, "geometries": {"from_node": "bounds"}, "reducer": spec["spatial"]},
        }
        _label(labels, str(info.get("variable") or dataset_id), column)
        if own == period:
            nodes.append(f"{column}_agg")
            like = like or f"{column}_agg"
        else:
            static.append((column, f"{column}_agg"))

    if like is None:
        raise ValueError(f"chap: at least one column must be a dataset at the {period} period")
    for column, agg in static:
        graph[f"{column}_{period}"] = {
            "process_id": "resample_cube_temporal",
            "arguments": {"data": {"from_node": agg}, "target": {"from_node": like}},
        }
        nodes.append(f"{column}_{period}")
    for column, spec in tables:
        graph[f"{column}_table"] = {
            "process_id": "load_table",
            "arguments": {"id": spec["table"], "value": spec["value"], "like": {"from_node": like}},
        }
        _label(labels, spec["value"], column)
        nodes.append(f"{column}_table")

    merged = nodes[0]
    for index, node in enumerate(nodes[1:], start=1):
        graph[f"merge_{index}"] = {
            "process_id": "merge_cubes",
            "arguments": {"cube1": {"from_node": merged}, "cube2": {"from_node": node}},
        }
        merged = f"merge_{index}"
    options = {"location_field": "feature_id", "period_type": period, "cube_labels": labels}
    graph["save"] = {
        "process_id": "save_result",
        "arguments": {"data": {"from_node": merged}, "format": "CHAPCSV", "options": options},
        "result": True,
    }
    return graph, options


def _label(labels: dict[str, str], variable: str, column: str) -> None:
    if variable in labels:
        raise ValueError(f"columns {labels[variable]} and {column} both have variable '{variable}'; merge_cubes needs distinct ones")
    labels[variable] = column


def run_instance(instance: Path, out_root: Path | None, url: str | None) -> Path:
    spec = yaml.safe_load((instance / "datasets.yaml").read_text(encoding="utf-8")) or {}
    chap = spec.get("chap")
    if not chap:
        raise ValueError(f"{instance.name}: datasets.yaml has no chap: block")
    config = yaml.safe_load((instance / "geoservice.yaml").read_text(encoding="utf-8"))
    iso3 = str(config["extent"]["country_code"]).upper()
    start, end = str(spec["start"]), str(spec["end"])

    server = None
    if url is None:
        server, url = _start_server(instance)
    try:
        client = httpx.Client(base_url=url, timeout=120)
        datasets = {d["dataset_id"]: d for d in _items(client.get("/datasets").json())}
        graph, _ = build_graph(chap, start, end, datasets)
        title = f"Chap CSV {iso3} {start}..{end}"
        job = client.post("/jobs", json={"title": title, "process": {"process_graph": graph}})
        job.raise_for_status()
        job_id = job.headers["openeo-identifier"]
        client.post(f"/jobs/{job_id}/results").raise_for_status()
        print(f"[{instance.name}] job {job_id} started", flush=True)
        status = _wait(client, job_id)
        if status != "finished":
            logs = client.get(f"/jobs/{job_id}/logs").text
            raise RuntimeError(f"job {job_id} ended {status}: {logs[:2000]}")
        csv = client.get(f"/jobs/{job_id}/results/result.csv")
        csv.raise_for_status()

        out = out_root / instance.name if out_root is not None else instance / "exports"
        out.mkdir(parents=True, exist_ok=True)
        name = chap.get("name", "chap_{iso3}_admin1_{adjective}").format(iso3=iso3, adjective=ADJECTIVE[chap["period"]])
        (out / f"{name}.csv").write_bytes(csv.content)
        (out / f"{name}.graph.json").write_text(json.dumps({"process_graph": graph}, indent=2), encoding="utf-8")
        _write_boundaries(client, chap["boundaries"], out / f"{name}.geojson")
        provenance = _provenance(instance, spec, config, datasets, chap, job_id, url)
        (out / f"{name}.provenance.json").write_text(json.dumps(provenance, indent=2, default=str), encoding="utf-8")
        rows = csv.text.count("\n") - 1
        print(f"[{instance.name}] {rows} rows -> {out / (name + '.csv')}", flush=True)
        return out / f"{name}.csv"
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)


def _items(listing: Any) -> list[dict[str, Any]]:
    return listing if isinstance(listing, list) else next(v for v in listing.values() if isinstance(v, list))


def _wait(client: httpx.Client, job_id: str, timeout: float = 3600) -> str:
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        status = client.get(f"/jobs/{job_id}").json()["status"]
        if status in ("finished", "error", "canceled"):
            return status
        time.sleep(3)
    return "timed out"


def _write_boundaries(client: httpx.Client, collection: str, target: Path) -> None:
    import io

    import geopandas as gpd

    data = client.get(f"/features/{collection}/data.parquet")
    data.raise_for_status()
    gpd.read_parquet(io.BytesIO(data.content)).to_crs(4326).to_file(target, driver="GeoJSON")


def _provenance(
    instance: Path,
    spec: dict[str, Any],
    config: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    chap: dict[str, Any],
    job_id: str,
    url: str,
) -> dict[str, Any]:
    used = [c.get("dataset") or c.get("table") for c in chap["columns"].values()] + [chap["boundaries"]]
    inputs = {}
    for dataset_id in used:
        info = datasets.get(dataset_id, {})
        inputs[dataset_id] = {
            key: info.get(key)
            for key in ("dataset_name", "source_dataset_id", "variable", "units", "period_type", "extent", "last_updated", "publication", "source", "license")
        }
    if chap["boundaries"] not in datasets:
        inputs[chap["boundaries"]] = httpx.get(f"{url}/features/{chap['boundaries']}", timeout=60).json()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "instance": instance.relative_to(INSTANCES).as_posix(),
        "repo": _git(),
        "locked": _locked(),
        "geoservice_yaml": config,
        "window": {"start": spec["start"], "end": spec["end"]},
        "chap": chap,
        "job_id": job_id,
        "inputs": inputs,
    }


def _git() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def _locked() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    wanted = {"open-geo-service", "open-geo-service-plugins", "openeo-processes-dask"}
    out = {}
    for package in lock.get("package", []):
        if package["name"] in wanted:
            source = package.get("source", {})
            out[package["name"]] = source.get("git") or package.get("version", "")
    return out


def _start_server(instance: Path) -> tuple[subprocess.Popen, str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "GEOSERVICE_CONFIG": str(instance / "geoservice.yaml")}
    log = open(instance / "data" / "chap-server.log", "w", encoding="utf-8")  # noqa: SIM115 — closed with the process
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "open_geo_service.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    began = time.monotonic()
    while time.monotonic() - began < 300:
        if server.poll() is not None:
            raise RuntimeError(f"the {instance.name} server exited; see {log.name} (is another writable server running?)")
        try:
            if httpx.get(url, timeout=5).status_code < 500:
                return server, url
        except httpx.TransportError:
            pass
        time.sleep(2)
    server.terminate()
    raise RuntimeError(f"the {instance.name} server did not start within 5 minutes; see {log.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="*", help="instance folders under instances/, e.g. lao")
    parser.add_argument("--all", action="store_true", help="every instance whose datasets.yaml has a chap: block")
    parser.add_argument("--out", type=Path, help="write to <out>/<instance>/ instead of the instance's exports/ folder")
    parser.add_argument("--url", help="use this running server instead of starting one (one instance only)")
    args = parser.parse_args()

    if args.all:
        dirs = sorted(
            p.parent
            for p in INSTANCES.glob("*/datasets.yaml")
            if (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("chap")
        )
    else:
        dirs = [(INSTANCES / name).resolve() for name in args.instances]
    if not dirs:
        parser.error("name at least one instance, or pass --all")
    if args.url and len(dirs) != 1:
        parser.error("--url serves one instance; name exactly one")

    status = 0
    for instance in dirs:
        try:
            run_instance(instance, args.out.resolve() if args.out else None, args.url)
        except Exception as exc:  # noqa: BLE001 — report and carry on with the next instance
            print(f"[{instance.name}] FAILED: {exc}", flush=True)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
