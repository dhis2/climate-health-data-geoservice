"""load_table: a table dataset's values as a vector cube shaped like another one.

Core has no process that brings a table dataset into a process graph. This one reads a published
table's value column and lays it out on the features and time steps of a `like` cube, such as an
`aggregate_spatial_weighted` result over the same feature collection. The output has the same
dimensions, geometries, feature ids and time labels as `like`, so `merge_cubes` combines the two
without aligning anything, and a feature or step the table has no row for is NaN.

Rows are matched by the table's feature id and period start against `like`'s `feature_id` and its
time labels, which Open Geo Service gives as the first day of each period. Rows outside `like`'s
features or steps are left out. The table read is recorded as a source of the run.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr

from open_geo_service.process import process


@process(
    summary="Load a table dataset's values onto the features and time steps of a vector cube",
    parameters={
        "id": {"description": "Table dataset id, as listed under /datasets."},
        "value": {"description": "The table column to load, one representing a value."},
        "like": {"description": "A vector cube with one datetime dimension, e.g. an aggregate_spatial_weighted result."},
    },
)
def load_table(id: str, value: str, like: xr.DataArray) -> xr.DataArray:
    """Return the table's *value* per feature and step of *like*, NaN where it has no row."""
    from open_geo_service.ingestions import services as ingestion_services
    from open_geo_service.shared.provenance import record_source
    from open_geo_service.shared.vectors import FEATURE_ID_COORD, vector_dim
    from open_geo_service.tables import store

    record = ingestion_services.published_table_artifacts_by_dataset().get(id)
    if record is None:
        raise ValueError(f"load_table: no published table dataset '{id}'")
    declared = store.schema_of(record)
    feature_column = declared.represented_by("feature_id")
    period_column = declared.represented_by("period_start")
    if feature_column is None or period_column is None:
        raise ValueError(f"load_table: table '{id}' has no feature or period column")
    if value not in declared.values:
        raise ValueError(f"load_table: '{value}' is not a value column of table '{id}'; its values are {list(declared.values)}")

    geometry_dim = vector_dim(like)
    times = [d for d in like.dims if pd.api.types.is_datetime64_any_dtype(like[d].dtype)]
    if geometry_dim is None or len(times) != 1 or FEATURE_ID_COORD not in like.coords:
        raise ValueError("load_table: `like` must be a vector cube with feature ids and one datetime dimension")
    time_dim = times[0]

    rows = store.read_rows(record).select([feature_column, period_column, value]).to_pandas()
    record_source(id, record)
    rows[period_column] = pd.to_datetime(rows[period_column])
    series = rows.set_index([feature_column, period_column])[value]
    if series.index.has_duplicates:
        raise ValueError(f"load_table: table '{id}' has several rows per feature and period for '{value}'")

    feature_ids = [str(f) for f in like[FEATURE_ID_COORD].values]
    steps = pd.DatetimeIndex(like[time_dim].values)
    grid = series.reindex(pd.MultiIndex.from_product([feature_ids, steps])).to_numpy(dtype="float64")
    frame = grid.reshape(len(feature_ids), len(steps))

    template = like.transpose(geometry_dim, time_dim, ...)
    if template.ndim != 2:
        raise ValueError(f"load_table: `like` must have only its feature and time dimensions, has {list(like.dims)}")
    out = xr.DataArray(frame, dims=(geometry_dim, time_dim), coords=template.coords, name=value, attrs={})
    return out.transpose(*like.dims)
