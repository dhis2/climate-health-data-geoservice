"""Natural Earth feature provider: one country's units from a Natural Earth layer.

Natural Earth publishes each layer as GeoJSON in its GitHub repository, tagged per release, so a
template pins `release` and the extract is reproducible. The country defaults to this instance's
`extent.country_code`, matched against the layer's `adm0_a3`.

Why Natural Earth: OpenDengue matched its admin-1 units to Natural Earth (`RNE_iso_code` is the
layer's `iso_3166_2`), so aggregating onto the same units lets disease and climate values share
one location code with no name matching.

**Duplicate ids.** Natural Earth occasionally gives one code to two units. In admin-1, Lao PDR's
Vientiane Prefecture and Vientiane Province are both `LA-VI`. Core requires unique ids, so
`duplicate_ids` decides: `error` (the default) refuses the extract and names the duplicates;
`dissolve` merges each group into one feature, its geometry the union and its text properties
joined with " + " (a differing number, such as `ne_id`, is left empty). Dissolving is what a source matched to Natural Earth by code implies: to
OpenDengue, `LA-VI` is both polygons.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import httpx
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from open_geo_service.extents.services import get_extent
from open_geo_service.features.providers import feature_provider

logger = logging.getLogger(__name__)

_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{release}/geojson/ne_{scale}_{layer}.geojson"
_COUNTRY_PROPERTY = "adm0_a3"
_DEFAULT_COLUMNS = ("iso_3166_2", "name", "name_en", "type_en", "adm1_code", "adm0_a3", "ne_id")


@feature_provider("natural_earth")
def natural_earth_features(
    *,
    release: str,
    layer: str = "admin_1_states_provinces",
    scale: str = "10m",
    country: str | None = None,
    id_property: str = "iso_3166_2",
    columns: Sequence[str] = _DEFAULT_COLUMNS,
    duplicate_ids: str = "error",
) -> tuple[dict[str, Any], str]:
    """Return one country's features from a Natural Earth layer, and the release read.

    Args:
        release: Natural Earth release tag, e.g. `v5.1.2`. Required, so an extract never changes
            under a template.
        layer: Layer name without the scale prefix, e.g. `admin_1_states_provinces`.
        scale: `10m`, `50m` or `110m`.
        country: ISO 3166-1 alpha-3 code to keep. Defaults to the instance's `extent.country_code`.
        id_property: Property every feature must carry, unique once duplicates are handled.
        columns: Properties to keep. A column the layer lacks is refused.
        duplicate_ids: `error` to refuse duplicate ids, or `dissolve` to merge them.
    """
    if duplicate_ids not in ("error", "dissolve"):
        raise ValueError(f"duplicate_ids must be 'error' or 'dissolve', got {duplicate_ids!r}")
    code = (country or _instance_country() or "").strip().upper()
    if len(code) != 3:
        raise ValueError(
            f"Natural Earth needs an ISO 3166-1 alpha-3 country code, got {code or 'none'!r}; "
            "set extent.country_code in the instance config or `country` in the template"
        )
    keep = list(dict.fromkeys([id_property, *columns]))

    url = _URL.format(release=release, scale=scale, layer=layer)
    logger.info("Downloading Natural Earth %s %s (%s) from %s", layer, scale, release, url)
    response = httpx.get(url, timeout=300, follow_redirects=True)
    response.raise_for_status()
    source = response.json()

    selected = [f for f in source["features"] if (f.get("properties") or {}).get(_COUNTRY_PROPERTY) == code]
    if not selected:
        raise ValueError(f"Natural Earth {layer} ({release}) has no features with {_COUNTRY_PROPERTY} = {code}")
    missing = [c for c in keep if c not in selected[0]["properties"]]
    if missing:
        raise ValueError(f"Natural Earth {layer} has no column(s) {missing}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, feature in enumerate(selected):
        identifier = feature["properties"].get(id_property)
        if identifier in (None, "", "-99"):
            raise ValueError(f"Natural Earth feature {index} ({feature['properties'].get('name')}) has no {id_property}")
        groups[str(identifier)].append(feature)

    duplicated = {k: [f["properties"].get("name") for f in v] for k, v in groups.items() if len(v) > 1}
    if duplicated and duplicate_ids == "error":
        raise ValueError(
            f"Natural Earth gives several {code} units the same {id_property}: {duplicated}; "
            "set duplicate_ids: dissolve in the template to merge them"
        )
    for identifier, names in duplicated.items():
        logger.warning("Dissolving %d Natural Earth units sharing %s=%s: %s", len(names), id_property, identifier, names)

    features = [_feature(members, keep, id_property) for members in groups.values()]
    logger.info("Extracted %d Natural Earth %s features for %s (release %s)", len(features), layer, code, release)
    return {"type": "FeatureCollection", "features": features}, release


def _feature(members: list[dict[str, Any]], keep: list[str], id_property: str) -> dict[str, Any]:
    """One feature from a group sharing an id: as is when alone, dissolved when several."""
    if len(members) == 1:
        props = members[0]["properties"]
        return {
            "type": "Feature",
            "properties": {c: props.get(c) for c in keep},
            "geometry": members[0]["geometry"],
        }
    properties: dict[str, Any] = {}
    for column in keep:
        values = list(dict.fromkeys(m["properties"].get(column) for m in members))
        if column == id_property or len(values) == 1:
            properties[column] = values[0]
        elif all(isinstance(v, str) for v in values):
            properties[column] = " + ".join(values)
        else:
            # A column must keep one type across features, so a differing number has no merged value.
            properties[column] = None
    geometry = unary_union([shape(m["geometry"]) for m in members])
    return {"type": "Feature", "properties": properties, "geometry": mapping(geometry)}


def _instance_country() -> str | None:
    extent = get_extent()
    return str(extent.get("country_code")) if extent and extent.get("country_code") else None
