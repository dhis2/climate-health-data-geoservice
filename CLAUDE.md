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
- **OpenDengue becomes a table dataset** (core PR #567, table templates and plugins, merged 2026-11-26). A vector collection can't hold a time series per unit, but core's tables can: rows keyed by `collection_id`, `feature_id` and `period_start`, with `feature_id` checked against a registered feature collection.
  - **Built (2026-11-28):** schema `plugins/tables/schemas/opendengue.yaml`, keyed (`collection_id`, `feature_id`, `period_start`); plugin `plugins/tables/opendengue.py` (`OpenDengueTablePlugin`, a `FieldsTablePlugin`); template `plugins/tables/opendengue_admin1.yaml` (`collection: natural_earth_admin1`, `period_type: monthly`, release pinned).
  - **One row per code and month.** Several OpenDengue names under one code are summed, their names kept in `source_names`. `adm_1_name` left the key so a join gives one value per unit.
  - **History against a snapshot:** `overrides` move a (code, name) to another code or drop it, with a reason; a code the collection lacks is dropped and logged (`unmatched: drop`), since the store refuses a whole write over one unknown feature. The plugin takes only the `T_res` matching the template's period type; a weekly table would be a second template.
  - **LAO verified:** 2479 rows, equal unit-month by unit-month to the raw CSV summed per code (99,934 cases); all 16 codes; 17 unit-months have no report (LA-PH 13, LA-XE 4).
  - A `FieldsTablePlugin` subclass reads the spatial extract.
  - One period type per table, so weekly rows would need a second template.
  - This will move to open-geo-service-plugins later.
- **Boundaries are Natural Earth 10m admin-1, not Overture.** OpenDengue matched its units to Natural Earth, and Chap needs one location code across every column, so climate is aggregated onto the same units. A `@feature_provider` in `plugins/vectors/` filters by `extent.country_code`, with ids from `iso_3166_2`.
- **Chap CSV: one process graph per country, generated from `datasets.yaml` by `scripts/chap.py` (built 2026-11-28).** Generated because it repeats per country.
  - **Every column is at the `chap.period`, or a single static period** (population's year) copied to every period with `resample_cube_temporal` after `aggregate_spatial_weighted` (`sum`). A finer dataset gets a **derived dataset** on the server instead of a time step in the graph: a template in `plugins/rasters/derived.yaml` whose `produced_by` recipe runs the `aggregate_to_period` workflow, so it is computed once, browsable, and refreshed by the server when its input updates. `ingest.py` creates it the first time (`derived:` list), as a batch job: running a graph without the job layer only returns the result and publishes nothing.
  - **Graph:** `load_features(boundaries)` → per column `load_collection` → `aggregate_spatial_weighted`; table columns via `load_table`; chained `merge_cubes`; `save_result` `CHAPCSV` with `cube_labels` (variable → column). Run as a batch job on the instance's server (started by `chap.py` on a free port, or `--url`).
  - **Outputs** in `<out>/<iso3>/`: `chap_<ISO3>_admin1_monthly.csv`, `.graph.json`, `.geojson` (boundaries) and `.provenance.json` (repo commit and dirty flag, locked Open Geo Service / plugins / openeo-processes-dask, instance config, `chap:` block, job id, each input as `/datasets` reports it). Core keeps no input provenance for a file export, only for a published dataset.
  - **Custom processes** (`plugins/processes/`), for gaps in core: `aggregate_period_start(data, period, reducer)` reduces to periods labelled by their first day, as stores are, with `sum(min_count=1)`; openeo-processes-dask's `aggregate_temporal_period` labels a month by its last day (values right), so `merge_cubes` refused to combine it with monthly stores, and its reducer is a child graph a workflow cannot parameterise. `aggregate_temporal` with explicit intervals is wrong outright: `groupby_bins` is right-closed, so January averages 2 Jan–1 Feb, and its labels are strings. `load_table(id, value, like)` lays a table's value column on a `like` vector cube's features and steps, NaN without a row.
  - **CHAPCSV** writes `199801` periods, which is what Chap expects (the published CSVs' `1998-01` was the mistake), and no `location_name`; neither matters.
  - **Verified on LAO:** the generated CSV equals the hand-written prototype's cell by cell (relative humidity within 3e-6, float32 storage).
  - Against the published LAO CSV (a reference, not the oracle): temperature and relative humidity agree (corr 0.998); rainfall correlates 0.9985 at 30.6× the published values, so the published rainfall was mm/d and ours is monthly totals; disease_cases are identical wherever both have a value, and ours also fill LA-VI, which the published CSV leaves empty for all 156 months; population is 1.1–4.7× the published per unit, summing to about 7.2 M for Lao PDR against about 4 M published.
- OpenDengue findings to reuse:
  - **Source:** the OpenDengue release `Spatial_extract_V1_3_1.zip` (55 MB zip, 500 MB CSV) at `github.com/OpenDengue/master-repo/data/releases/`. Filter with `S_res == "Admin1"`.
  - **Matching:** don't match on names. `RNE_iso_code` equals Natural Earth 10m admin-1 `iso_3166_2` exactly, in both directions, for LAO (16 units), THA (77) and VNM (63).
  - **Keys:** (`RNE_iso_code`, `adm_1_name`, `calendar_start_date`, `calendar_end_date`) is unique. Several names share one code:
    - TH-25: PRACHIN BURI (1993–2002) then PHACHINBURI (2003–2022), a rename; summing is right.
    - LA-VI: VIENTIANE CAPITAL and VIENTIANE CAPITAL [PREFECTURE] over the same 156 months with different values, plus XAISOMBOUN (1998–2005). All lie in Natural Earth's dissolved LA-VI, so summing is right.
    - VN-30: GIA LAI and HA TAY over the same months. Ha Tay merged into Ha Noi in 2008, so the template overrides HA TAY to VN-HN.
  - **Resolutions:** at Admin1, LAO, THA and VNM are Month only (LAO 1998–2010, THA 1993–2022, VNM 1994-02 to 2010). Week and Year occur only at Admin0.
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
| `rainfall` | ERA5-Land total precipitation, "mm" | built-in `era5land_precipitation_monthly` (mm, monthly total since core #563; it was mm/d before) |
| `mean_relative_humidity` | Magnus (a=6.112, b=17.67, c=243.5) on monthly-mean T and Td | built-in `era5land_relative_humidity_daily`. Use it rather than writing our own. Daily to monthly harmonisation happens in a later step, together with the other datasets. |
| `population` | WorldPop 2020, applied to every period | built-in `worldpop_population_global2_100m` (2015–2030), year 2020. This may be a different WorldPop release than the one published. |
| `disease_cases` | OpenDengue | table dataset (see decisions) |
| boundaries | attributed to OCHA COD-AB, but the fields `shapeISO`/`shapeName` are geoBoundaries' | Natural Earth 10m admin-1 (see decisions). An Overture admin-1 template came first and was dropped. |

- **What Open Geo Service produces takes precedence over the published CSVs (2026-11-27).** They are a reference, not the oracle. For example, the published rainfall looks like a mean daily rate (LAO Jan 1998 ≈ 0.03), while ours is now a monthly total in mm.
- **Core bug: the monthly precipitation sum stores no-data cells as 0.** At `12f79db`, `ERA5LandEDHMonthlyFromDailyPlugin` sums with `skipna` and no `min_count`, so sea cells ERA5-Land leaves NaN become 0 mm (667 of 7031 cells in LAO's bbox; land values are exact). Coastal admin-1 units would average in those zeros. Report it upstream and re-ingest once fixed.

## Local setup notes
- Windows machine with no `make`. Run `uv sync`, then set `$env:GEOSERVICE_CONFIG = "instances/<iso3>/geoservice.yaml"` and run `uv run uvicorn open_geo_service.main:app --host 127.0.0.1 --port <port>`.
- `.env` holds credentials only. Copy the values from an existing open-geo-service worktree's `.env` and never print them.
  - Core reads `EDH_API_KEY` (Earth Data Hub: the monthly ERA5-Land built-ins) and `CDS_API_KEY` (CDS: `era5land_relative_humidity_daily`).
- **Ingesting:** each instance lists its datasets in `instances/<iso3>/datasets.yaml`, with a window plus raster and vector ids. Run `uv run python scripts/ingest.py lao` (or `--all`, `--only <id>`). It starts one subprocess per instance with `GEOSERVICE_CONFIG` set, calls `execute_ingestion` for rasters and `refresh_feature_collection_from_provider` for vectors, and doesn't need a server.
- **Chap CSV:** `uv run python scripts/chap.py lao` (or `--all`) writes to `instances/<iso3>/exports/` (gitignored); `--out <folder>` writes elsewhere. With no `--url` it starts the instance's server itself, so stop any writable server on that instance first.
- **Upgrading core:** `uv lock --upgrade-package open-geo-service --upgrade-package open-geo-service-plugins`, then `uv sync`. If the data folder layout changed, a writable server refuses to start: back up `data/`, stop every server, and run `uv run python -m open_geo_service.migrate_layout` (with `--dry-run` first) with `GEOSERVICE_CONFIG` set. A dataset whose definition changed has no delete API: remove its `catalog/` folder and its entry in `artifacts/records.json`, then ingest again.
- **One writer per instance.** Core holds a writer lease on each `data_dir`, so a writable server can't run while `ingest.py` does. To browse during ingestion, serve a copy of the config with `read_only: true`. It picks up newly published data without a restart.
- **A failed ingestion resumes.** Re-running continues from the last committed period, and a dataset is listed only after a run completes.
  - `era5land_relative_humidity_daily` (CDS/ARCO) runs at about 117 days per minute for LAO and holds about 256 MiB per variable in cache.
  - Its `MemoryError`s on 2026-11-25 came from this machine being near its Windows commit limit (RAM plus a 4 GB pagefile), not from the reader.
