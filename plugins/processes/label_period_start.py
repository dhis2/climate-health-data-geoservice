"""label_period_start: label each step of a cube by the first day of its period.

Open Geo Service stores a period by its first day (`1998-01-01` for January 1998), but
openeo-processes-dask's `aggregate_temporal_period` labels a month by its last day
(`1998-01-31`). `merge_cubes` then refuses to combine a monthly store with a daily store
aggregated to months, as their labels differ. This moves each label to the start of its period,
keeping it a date, so the two line up. Values are untouched.

`rename_labels` cannot do it: it turns the dates into strings, which still differ from a store's
dates.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from open_geo_service.process import process

_FREQ = {"day": "D", "week": "W-MON", "month": "M", "season": "Q-NOV", "year": "Y"}


@process(
    summary="Label each time step by the first day of its period",
    parameters={
        "data": {"description": "A data cube with one datetime dimension."},
        "period": {"description": "The period each step stands for: day, week, month, season or year."},
    },
)
def label_period_start(data: xr.DataArray | xr.Dataset, period: str) -> xr.DataArray | xr.Dataset:
    """Return *data* with each step of its datetime dimension labelled by its period's first day."""
    if period not in _FREQ:
        raise ValueError(f"label_period_start: period must be one of {list(_FREQ)}, got {period!r}")
    dims = [d for d in data.dims if pd.api.types.is_datetime64_any_dtype(data[d].dtype)]
    if len(dims) != 1:
        raise ValueError(f"label_period_start: expected one datetime dimension, found {dims}")
    dim = dims[0]
    starts = pd.DatetimeIndex(data[dim].values).to_period(_FREQ[period]).start_time
    if starts.has_duplicates:
        raise ValueError(f"label_period_start: several steps fall in one {period}; aggregate them first")
    return data.assign_coords({dim: starts.values.astype("datetime64[ns]")})
