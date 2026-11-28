"""aggregate_period_start: reduce a cube's steps to periods, each labelled by its first day.

The standard `aggregate_temporal_period` (openeo-processes-dask) labels a month by its last day,
while Open Geo Service stores label a period by its first, and its reducer is a child process
graph, so a workflow cannot pick it from a parameter. This takes the reducer by name and labels
each period by its first day, so its output lines up with stored datasets in `merge_cubes` and can
be published as one. A sum over a period with no valid values is NaN, not 0 (`min_count=1`).
"""

from __future__ import annotations

import pandas as pd
import xarray as xr

from open_geo_service.process import process

# pandas period frequencies: ISO weeks (Monday to Sunday), months, meteorological seasons
# (December to February, ...) and calendar years.
_FREQ = {"week": "W-SUN", "month": "M", "season": "Q-NOV", "year": "Y"}
_REDUCERS = ("mean", "sum", "min", "max")


@process(
    summary="Aggregate a cube's steps to periods, each labelled by its first day",
    parameters={
        "data": {"description": "A data cube with one datetime dimension."},
        "period": {"description": "week (ISO, from Monday), month, season (DJF, MAM, JJA, SON) or year."},
        "reducer": {"description": "How the steps within a period are combined: mean, sum, min or max."},
    },
)
def aggregate_period_start(data: xr.DataArray | xr.Dataset, period: str, reducer: str) -> xr.DataArray | xr.Dataset:
    """Return *data* reduced to one step per *period*, labelled by the period's first day."""
    if period not in _FREQ:
        raise ValueError(f"aggregate_period_start: period must be one of {list(_FREQ)}, got {period!r}")
    if reducer not in _REDUCERS:
        raise ValueError(f"aggregate_period_start: reducer must be one of {list(_REDUCERS)}, got {reducer!r}")
    dims = [d for d in data.dims if pd.api.types.is_datetime64_any_dtype(data[d].dtype)]
    if len(dims) != 1:
        raise ValueError(f"aggregate_period_start: expected one datetime dimension, found {dims}")
    dim = dims[0]
    starts = pd.DatetimeIndex(data[dim].values).to_period(_FREQ[period]).start_time
    key = xr.DataArray(starts.values.astype("datetime64[ns]"), dims=dim, name=dim)
    groups = data.groupby(key)
    if reducer == "sum":
        result = groups.sum(dim, min_count=1, keep_attrs=True)
    else:
        result = getattr(groups, reducer)(dim, keep_attrs=True)
    return result
