# climate-health-data-geoservice

## Purpose
A multi-instance [Open Geo Service](https://github.com/dhis2/open-geo-service) repo: one instance per country, sharing one set of plugins and one locked Open Geo Service version. It ingests the climate and health data behind the Chap datasets published in [dhis2/climate-health-data](https://github.com/dhis2/climate-health-data).

## Layout
```
pyproject.toml, uv.lock   # open-geo-service[server] + open-geo-service-plugins, both on git main, locked
.env                      # credentials only, shared by all instances (never commit, never print)
plugins/                  # shared; only vectors/ for now (add rasters/, processes/, workflows/ when needed)
instances/<iso3>/         # e.g. lao — one per country: climate, boundaries and every disease table
  geoservice.yaml         # id: <iso3>, extent (bbox + country_code), data_dir: ./data, plugins_dir: ../../plugins
  datasets.yaml           # what scripts/ingest.py ingests
  data/                   # Zarr stores, gitignored
```
- The pattern follows turban's instance repos. The closest is `dhis2/nepal-geo-service-test` (uv, writable, localhost). `dhis2/nepal-geo-service-demo` is the Docker and read-only variant for when this gets hosted. Core's guide is `docs/instance_guide.md`.
- Datasets come from three places: core built-ins, then installed `open-geo-service-plugins` (found via entry point), then `plugins/` here. A later source wins on the same id.
- `data_dir` resolves relative to the config file. Core reads the config once per process, so each instance is its own server or process, chosen with `GEOSERVICE_CONFIG`.
- Instances and ports: `lao` 8011, `tha` 8012, `vnm` 8013. The bboxes are approximate for now; derive them from the admin-1 boundaries later.

## Decisions (2026-11-25)
- **One instance per country, shared by every disease (2026-11-27).** Separate instances per disease would duplicate all the climate data. Each disease is a table in the country's instance, joined to its boundaries by feature id. Tables may cover different years, so a window per table goes in `datasets.yaml` and the climate window spans them all.
- **This repo is separate from `climate-health-data`.** Outputs (Chap CSV, GeoJSON, schema) are exported externally, not stored here. Export comes later. The first goal is just the three instances holding the data.
- **OpenDengue becomes a table dataset, once core PR #567 (table templates and plugins) merges.** A vector collection can't hold a time series per unit, but core's tables can: rows keyed by `collection_id`, `feature_id` and `period_start`, with `feature_id` checked against a registered feature collection.
  - The plan is a custom schema in `plugins/tables/schemas/opendengue.yaml` whose key includes `adm_1_name`, plus a template in `plugins/tables/` with `collection:` naming the Natural Earth admin-1 collection and `period_type: monthly`.
  - A `FieldsTablePlugin` subclass reads the spatial extract.
  - One period type per table, so weekly rows would need a second template.
  - This will move to open-geo-service-plugins later.
- **Boundaries are Natural Earth 10m admin-1, not Overture.** OpenDengue matched its units to Natural Earth, and Chap needs one location code across every column, so climate is aggregated onto the same units. A `@feature_provider` in `plugins/vectors/` filters by `extent.country_code`, with ids from `iso_3166_2`.
- **Chap CSV comes from a process graph generated from `datasets.yaml`, not written by hand.** It's needed because we repeat it per country.
  - **Per dataset:** `load_collection`, then a temporal step, then `aggregate_spatial_weighted`.
  - **Temporal step:** `aggregate_temporal_period` if the dataset is finer than the target period, nothing if it matches, and `resample_cube_temporal` (nearest timestamp; broadcasts a single year) if it's coarser.
  - **Combining:** chain `merge_cubes`, then `save_result` with `CHAPCSV` and `cube_labels`.
  - **Provenance:** save the generated graph next to the output; core's job record adds the input snapshots.
  - **Not covered:** tables can't be loaded into a graph yet, so `disease_cases` is joined outside the graph by (`location`, `time_period`).
- OpenDengue findings to reuse:
  - **Source:** the OpenDengue release `Spatial_extract_V1_3_1.zip` (55 MB zip, 500 MB CSV) at `github.com/OpenDengue/master-repo/data/releases/`. Filter with `S_res == "Admin1"`.
  - **Matching:** don't match on names. `RNE_iso_code` equals Natural Earth 10m admin-1 `iso_3166_2` exactly, in both directions, for LAO (16 units), THA (77) and VNM (63).
  - **Keys:** (`RNE_iso_code`, `adm_1_name`, `calendar_start_date`, `calendar_end_date`) is unique. Several names share one code: LA-VI covers VIENTIANE CAPITAL and XAISOMBOUN; TH-25 covers PRACHIN BURI and PHACHINBURI; VN-30 covers GIA LAI and HA TAY. HA TAY on VN-30 looks like an upstream error, since Ha Tay merged into Hanoi.
  - **Mixed resolutions:** T_res mixes Month, Week and Year.
  - **Reference code:** `dhis2-chap/chap-core` has `chap_core/data/open_dengue.py`.
- **The goal is reproducibility.** Today `climate-health-data` can't be replicated. Each output should record what produced it: the Open Geo Service and plugins commits from `uv.lock`, plus the instance config.
- Ingest the whole window in one request, not in increments (CLIM-899), and never use `overwrite` (CLIM-898).

## What climate-health-data has (the replication target)
- `lao/`, `tha/` and `vnm/`, each with `chap_<ISO3>_admin1_monthly.{csv,geojson}` and `_schema.json`. Chap depends on these names.
- **Time windows:**
  - LAO: 1998-01 to 2010-12
  - VNM: 1998-01 to 2010-12
  - THA: 1993-01 to 2022-12
- **CSV:** one row per (`time_period`, `location`) on a full grid, with NaN left as NaN.
  - Columns: `time_period, location, disease_cases, population, location_name, rainfall, mean_temperature, mean_relative_humidity`.
  - `location` is `shapeISO`.
- **Variables and the matching Open Geo Service datasets:**

| Column | Published source | Open Geo Service dataset |
|---|---|---|
| `mean_temperature` | ERA5-Land 2m temperature, °C | built-in `era5land_temperature_monthly` (degC, monthly mean, Earth Data Hub) |
| `rainfall` | ERA5-Land total precipitation, "mm" | built-in `era5land_precipitation_monthly` (mm/d, monthly mean daily rate) |
| `mean_relative_humidity` | Magnus (a=6.112, b=17.67, c=243.5) on monthly-mean T and Td | built-in `era5land_relative_humidity_daily`. Use it rather than writing our own. Daily to monthly harmonisation happens in a later step, together with the other datasets. |
| `population` | WorldPop 2020, applied to every period | built-in `worldpop_population_global2_100m` (2015–2030), year 2020. This may be a different WorldPop release than the one published. |
| `disease_cases` | OpenDengue | table dataset, waiting on core #567 (see decisions) |
| boundaries | attributed to OCHA COD-AB, but the fields `shapeISO`/`shapeName` are geoBoundaries' | Natural Earth 10m admin-1 (see decisions). An Overture admin-1 template came first and was dropped. |

- **Rainfall units are unclear.** The schema says "mm", but the values look like a mean daily rate. Examples: LAO Jan 1998 is about 0.03, and Narathiwat Dec 2022 is 14.4, which would be about 450 mm per month. If so, the published values match the built-in mm/d dataset, not a monthly sum. Confirm when comparing.

## Local setup notes
- Windows machine with no `make`. Run `uv sync`, then set `$env:GEOSERVICE_CONFIG = "instances/<iso3>/geoservice.yaml"` and run `uv run uvicorn open_geo_service.main:app --host 127.0.0.1 --port <port>`.
- `.env` holds credentials only. Copy the values from an existing open-geo-service worktree's `.env` and never print them.
  - Core reads `EDH_API_KEY` (Earth Data Hub: the monthly ERA5-Land built-ins) and `CDS_API_KEY` (CDS: `era5land_relative_humidity_daily`).
- **Ingesting:** each instance lists its datasets in `instances/<iso3>/datasets.yaml`, with a window plus raster and vector ids. Run `uv run python scripts/ingest.py lao` (or `--all`, `--only <id>`). It starts one subprocess per instance with `GEOSERVICE_CONFIG` set, calls `execute_ingestion` for rasters and `refresh_feature_collection_from_provider` for vectors, and doesn't need a server.
- **One writer per instance.** Core holds a writer lease on each `data_dir`, so a writable server can't run while `ingest.py` does. To browse during ingestion, serve a copy of the config with `read_only: true`. It picks up newly published data without a restart.
- **A failed ingestion resumes.** Re-running continues from the last committed period, and a dataset is listed only after a run completes.
  - `era5land_relative_humidity_daily` (CDS/ARCO) runs at about 117 days per minute for LAO and holds about 256 MiB per variable in cache.
  - Its `MemoryError`s on 2026-11-25 came from this machine being near its Windows commit limit (RAM plus a 4 GB pagefile), not from the reader.
