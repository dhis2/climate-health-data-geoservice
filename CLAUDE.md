# climate-health-data-geoservice

## Purpose
A multi-instance [Open Geo Service](https://github.com/dhis2/open-geo-service) repo: one instance per country, sharing one set of plugins and one locked Open Geo Service version. It ingests the climate and health data behind the Chap datasets published in [dhis2/climate-health-data](https://github.com/dhis2/climate-health-data).

## Layout
```
pyproject.toml, uv.lock   # open-geo-service[server] + open-geo-service-plugins, both on git main, locked
.env                      # credentials only, shared by all instances (never commit, never print)
plugins/                  # shared; only vectors/ for now (add rasters/, processes/, workflows/ when needed)
instances/<disease>/<iso3>/   # e.g. opendengue/lao — grouped by disease data source, then country
  geoservice.yaml         # id: <disease>-<iso3>, extent (bbox + country_code), data_dir: ./data, plugins_dir: ../../../plugins
  data/                   # Zarr stores, gitignored
```
- The pattern follows turban's instance repos. The closest is `dhis2/nepal-geo-service-test` (uv, writable, localhost). `dhis2/nepal-geo-service-demo` is the Docker and read-only variant for when this gets hosted. Core's guide is `docs/instance_guide.md`.
- Datasets come from three places: core built-ins, then installed `open-geo-service-plugins` (found via entry point), then `plugins/` here. A later source wins on the same id.
- `data_dir` resolves relative to the config file. Core reads the config once per process, so each instance is its own server or process, chosen with `GEOSERVICE_CONFIG`.
- Instances and ports: `opendengue/lao` 8011, `opendengue/tha` 8012, `opendengue/vnm` 8013. The bboxes are approximate for now; derive them from the admin-1 boundaries later.

## Decisions (2026-11-25)
- **This repo is separate from `climate-health-data`.** Outputs (Chap CSV, GeoJSON, schema) are exported externally, not stored here. Export comes later. The first goal is just the three instances holding the data.
- **The OpenDengue disease plugin** goes in [dhis2-chap/open-geo-service-plugins](https://github.com/dhis2-chap/open-geo-service-plugins), not core and not here. It doesn't exist yet; `dhis2-chap/chap-core` has `chap_core/data/open_dengue.py` as a reference.
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
| `disease_cases` | OpenDengue | not available yet (see decisions) |
| boundaries | attributed to OCHA COD-AB, but the fields `shapeISO`/`shapeName` are geoBoundaries' | built-in Overture provider (a template in `plugins/vectors/`, as in `nepal-geo-service-test`). Not geoBoundaries. |

- **Rainfall units are unclear.** The schema says "mm", but the values look like a mean daily rate. Examples: LAO Jan 1998 is about 0.03, and Narathiwat Dec 2022 is 14.4, which would be about 450 mm per month. If so, the published values match the built-in mm/d dataset, not a monthly sum. Confirm when comparing.

## Local setup notes
- Windows machine with no `make`. Run `uv sync`, then set `$env:GEOSERVICE_CONFIG = "instances/<disease>/<iso3>/geoservice.yaml"` and run `uv run uvicorn open_geo_service.main:app --host 127.0.0.1 --port <port>`.
- `.env` holds credentials only. Copy the values from an existing open-geo-service worktree's `.env` and never print them.
  - CDS keys are `ECMWF_DATASTORES_URL`/`ECMWF_DATASTORES_KEY`.
  - The monthly ERA5-Land built-ins read Earth Data Hub (DestinE). Check whether that needs its own credentials.
- To ingest without a server: `open_geo_service.ingestions.processes.execute_ingestion(dataset_id=..., start=..., end=..., publish=True)`, with `GEOSERVICE_CONFIG` set, one process per instance.
