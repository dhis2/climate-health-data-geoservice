# climate-health-data-geoservice

[Open Geo Service](https://github.com/dhis2/open-geo-service) instances that build the Chap
climate and health datasets published in
[dhis2/climate-health-data](https://github.com/dhis2/climate-health-data).

One repo, many instances. Instances are grouped by disease data source, then country
(`instances/opendengue/lao/`). Each has its own config and data, and all of them share one
set of plugins and one locked Open Geo Service version.

## Layout

```
pyproject.toml, uv.lock     # open-geo-service + open-geo-service-plugins, locked
.env                        # credentials only, shared by all instances (copy .env.example)
plugins/                    # shared by every instance; add rasters/, processes/, workflows/ when needed
  vectors/                  #   vector templates (.yaml) + feature providers (.py)
instances/
  <disease>/<iso3>/         # e.g. opendengue/lao
    geoservice.yaml         # id, extent (bbox + country_code), data_dir, plugins_dir
    data/                   # this instance's stores (gitignored)
```

Datasets come from three places, later ones overriding earlier ones on the same id: Open Geo
Service's built-ins, the installed
[open-geo-service-plugins](https://github.com/dhis2-chap/open-geo-service-plugins), then
`plugins/` here.

## Setup

```powershell
uv sync
Copy-Item .env.example .env    # then fill in credentials
```

## Running an instance

`GEOSERVICE_CONFIG` picks the instance. Open Geo Service reads the config once per process,
so each instance is its own server, and several can run side by side on different ports.

```powershell
$env:GEOSERVICE_CONFIG = "instances/opendengue/lao/geoservice.yaml"
uv run uvicorn open_geo_service.main:app --host 127.0.0.1 --port 8011
```

```bash
GEOSERVICE_CONFIG=instances/opendengue/lao/geoservice.yaml \
  uv run uvicorn open_geo_service.main:app --host 127.0.0.1 --port 8011
```

| Instance | Port |
| --- | --- |
| `opendengue/lao` | 8011 |
| `opendengue/tha` | 8012 |
| `opendengue/vnm` | 8013 |

Instances are writable, so bind them to `127.0.0.1` only.

## Adding a country

Copy an existing `instances/<disease>/<iso3>/` folder, then set `id` (`<disease>-<iso3>`), `name`,
`extent.bbox` and `extent.country_code`, and give it the next free port.

## Upgrading Open Geo Service

```powershell
uv lock --upgrade; uv sync
```
