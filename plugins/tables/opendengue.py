"""OpenDengue table plugin: reported dengue cases for one country, joined to a feature collection by code.

OpenDengue (https://opendengue.org) publishes its data as fixed releases on GitHub, one zip per
extract (national, highest spatial or highest temporal resolution). The plugin downloads the
pinned release once into the instance cache, keeps the records of this instance's country at one
spatial level (`S_res`, e.g. `Admin1`) and of the template's temporal resolution (`T_res`
`Month` for a monthly template), and joins them to the template's feature collection by code,
never by name. At admin 1 that code is `RNE_iso_code`, which OpenDengue matched to Natural
Earth's `iso_3166_2`.

**History against a snapshot.** OpenDengue reports units as they were at the time; a boundary
collection is one recent edition. Three things follow, each handled explicitly:

- Several names under one code (a renamed province, or units the boundaries give one code) are
  summed into one row per code and period, their names kept in `source_names`.
- A record coded wrongly upstream, or reported under a unit since merged into another, is moved
  by an `overrides` entry naming its code and name, with the reason. An override that matches
  nothing is logged, so a stale one is noticed.
- A code the collection lacks is dropped and logged with its records and cases
  (`unmatched: drop`), or refuses the ingestion (`unmatched: error`). The table store would refuse
  the whole write for one unknown feature, so this is decided here.

A unit later split in two cannot be apportioned to today's polygons; its records stay on the code
they were reported under, or are dropped if the collection no longer has it.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from open_geo_service import config as api_config
from open_geo_service.extents.services import get_extent
from open_geo_service.tables.plugin import FieldsTablePlugin

logger = logging.getLogger(__name__)

_URL = "https://github.com/OpenDengue/master-repo/raw/main/data/releases/V{release}/{file}"
_EXTRACTS = {"national": "National", "spatial": "Spatial", "temporal": "Temporal"}
_T_RES = {"weekly": "Week", "monthly": "Month", "yearly": "Year"}
_NAME_FIELD = {"Admin0": "adm_0_name", "Admin1": "adm_1_name", "Admin2": "adm_2_name"}
_CHUNK_ROWS = 500_000


class OpenDengueTablePlugin(FieldsTablePlugin):
    """Rows of the `opendengue` schema from one OpenDengue release.

    Args (the template's `ingestion.params`):
        release: OpenDengue release, e.g. `1.3.1`. Required, so a table never changes under a template.
        collection: The feature collection the rows join to; the template's `collection` again, as
            a plugin is not given it.
        extract: `spatial` (default), `temporal` or `national`.
        level: OpenDengue's `S_res` to keep: `Admin0`, `Admin1` (default) or `Admin2`.
        id_field: OpenDengue column holding the collection's feature ids, `RNE_iso_code` by default.
        country: ISO 3166-1 alpha-3 code (`ISO_A0`). Defaults to the instance's `extent.country_code`.
        unmatched: `drop` (default) or `error`, for codes the collection lacks.
        overrides: Entries `{country, code, name, to, reason}`: in that country (`ISO_A0`), records
            with that code and unit name move to the code `to`, or are dropped when `to` is null.
            One template serves every country, so an entry applies only in its own.
        name_property: The collection's property that names a unit, written to `feature_name`;
            `name` by default.
    """

    def __init__(
        self,
        *,
        release: str,
        collection: str,
        extract: str = "spatial",
        level: str = "Admin1",
        id_field: str = "RNE_iso_code",
        country: str | None = None,
        unmatched: str = "drop",
        overrides: Sequence[Mapping[str, Any]] = (),
        name_property: str = "name",
        **context: Any,
    ) -> None:
        super().__init__(**context)
        if extract not in _EXTRACTS:
            raise ValueError(f"extract must be one of {list(_EXTRACTS)}, got {extract!r}")
        if level not in _NAME_FIELD:
            raise ValueError(f"level must be one of {list(_NAME_FIELD)}, got {level!r}")
        if self.period_type not in _T_RES:
            raise ValueError(f"OpenDengue has no {self.period_type} data; use one of {list(_T_RES)}")
        if unmatched not in ("drop", "error"):
            raise ValueError(f"unmatched must be 'drop' or 'error', got {unmatched!r}")
        for entry in overrides:
            if not {"country", "code", "name", "reason"} <= set(entry) or "to" not in entry:
                raise ValueError(
                    f"an override needs country, code, name, to (a code, or null to drop) and reason: {dict(entry)}"
                )
        self.release = str(release)
        self.collection = collection
        self.extract = extract
        self.level = level
        self.id_field = id_field
        self.country = country
        self.unmatched = unmatched
        self.overrides = [dict(entry) for entry in overrides]
        self.name_property = name_property

    def records(self, start: date | None, end: date | None) -> Iterator[tuple[str, Mapping[str, Any]]]:
        country = (self.country or _instance_country() or "").strip().upper()
        if len(country) != 3:
            raise ValueError(
                f"OpenDengue needs an ISO 3166-1 alpha-3 country code, got {country or 'none'!r}; "
                "set extent.country_code in the instance config or `country` in the template"
            )
        features = _collection_names(self.collection, self.name_property)
        frame = self._read(country)
        frame = self._apply_overrides(frame, country)
        frame = self._drop_unmatched(frame, set(features))

        summed = (
            frame.groupby([self.id_field, "calendar_start_date"], sort=True)
            .agg(
                dengue_total=("dengue_total", lambda v: v.sum(min_count=1)),
                source_names=(_NAME_FIELD[self.level], _joined),
                source_rows=("dengue_total", "size"),
                case_definition=("case_definition_standardised", _joined),
            )
            .reset_index()
        )
        logger.info(
            "OpenDengue %s %s: %d records for %s at %s, summed into %d rows for %d units",
            self.extract,
            self.release,
            len(frame),
            country,
            self.level,
            len(summed),
            summed[self.id_field].nunique(),
        )
        for row in summed.itertuples(index=False):
            code, period = getattr(row, self.id_field), row.calendar_start_date
            yield (
                f"OpenDengue {code} {period}",
                {
                    "feature_id": code,
                    "feature_name": features.get(code) or "",
                    "period_start": period,
                    "dengue_total": "" if pd.isna(row.dengue_total) else repr(float(row.dengue_total)),
                    "source_names": row.source_names,
                    "source_rows": str(row.source_rows),
                    "case_definition": row.case_definition,
                    "release": self.release,
                },
            )

    def _read(self, country: str) -> pd.DataFrame:
        """The release's records for *country* at this level and temporal resolution."""
        name_field = _NAME_FIELD[self.level]
        fields = list(
            dict.fromkeys(
                [
                    "ISO_A0",
                    "S_res",
                    "T_res",
                    self.id_field,
                    name_field,
                    "calendar_start_date",
                    "calendar_end_date",
                    "dengue_total",
                    "case_definition_standardised",
                ]
            )
        )
        path = self._download()
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"{path.name} holds {len(members)} CSV files, expected one")
            with archive.open(members[0]) as handle:
                chunks = pd.read_csv(handle, usecols=fields, dtype=str, chunksize=_CHUNK_ROWS, keep_default_na=False)
                kept = [chunk[(chunk["ISO_A0"] == country) & (chunk["S_res"] == self.level)] for chunk in chunks]
        frame = pd.concat(kept, ignore_index=True)
        if frame.empty:
            raise ValueError(f"OpenDengue {self.release} has no {self.level} records for {country}")

        wanted = _T_RES[self.period_type]
        other = frame[frame["T_res"] != wanted]
        if not other.empty:
            logger.info(
                "Leaving out %d %s records of %s at other temporal resolutions %s; a %s template takes only %s",
                len(other),
                self.level,
                country,
                sorted(other["T_res"].unique()),
                self.period_type,
                wanted,
            )
        frame = frame[frame["T_res"] == wanted].copy()
        if frame.empty:
            raise ValueError(f"OpenDengue {self.release} has no {wanted} {self.level} records for {country}")
        frame["dengue_total"] = pd.to_numeric(frame["dengue_total"].replace("", None), errors="raise")
        frame[self.id_field] = frame[self.id_field].str.strip()
        return frame

    def _apply_overrides(self, frame: pd.DataFrame, country: str) -> pd.DataFrame:
        name_field = _NAME_FIELD[self.level]
        for entry in self.overrides:
            if str(entry["country"]).upper() != country:
                continue
            hit = (frame[self.id_field] == entry["code"]) & (frame[name_field] == entry["name"])
            count = int(hit.sum())
            if not count:
                logger.warning("OpenDengue override %s/%s matches no records; is it stale?", entry["code"], entry["name"])
                continue
            if entry["to"] is None:
                logger.info("Dropping %d records of %s/%s: %s", count, entry["code"], entry["name"], entry["reason"])
                frame = frame[~hit]
            else:
                logger.info(
                    "Moving %d records of %s/%s to %s: %s", count, entry["code"], entry["name"], entry["to"], entry["reason"]
                )
                frame.loc[hit, self.id_field] = str(entry["to"])
        return frame

    def _drop_unmatched(self, frame: pd.DataFrame, known: set[str]) -> pd.DataFrame:
        missing = ~frame[self.id_field].isin(known)
        if not missing.any():
            return frame
        name_field = _NAME_FIELD[self.level]
        lost = (
            frame[missing]
            .assign(code=lambda f: f[self.id_field].replace("", "(none)"))
            .groupby("code")
            .agg(names=(name_field, _joined), records=("dengue_total", "size"), cases=("dengue_total", "sum"))
        )
        detail = "; ".join(f"{code} ({r.names}): {r.records} records, {r.cases:g} cases" for code, r in lost.iterrows())
        if self.unmatched == "error":
            raise ValueError(f"OpenDengue codes not in feature collection '{self.collection}': {detail}")
        logger.warning("Dropping OpenDengue codes not in feature collection '%s': %s", self.collection, detail)
        return frame[~missing]

    def _download(self) -> Path:
        """The release zip, downloaded once into the instance cache."""
        file = f"{_EXTRACTS[self.extract]}_extract_V{self.release.replace('.', '_')}.zip"
        data_dir = api_config.get_data_dir()
        folder = (data_dir if data_dir is not None else Path.home() / ".cache" / "geoservice") / "cache" / "opendengue"
        target = folder / file
        if target.is_file():
            return target
        import httpx

        folder.mkdir(parents=True, exist_ok=True)
        url = _URL.format(release=self.release, file=file)
        logger.info("Downloading OpenDengue %s from %s", file, url)
        partial = target.with_suffix(".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for block in response.iter_bytes():
                    out.write(block)
        partial.replace(target)
        return target


def _joined(values: pd.Series) -> str:
    return " + ".join(sorted({str(v) for v in values if str(v).strip()}))


def _collection_names(collection_id: str, name_property: str) -> dict[str, str | None]:
    """The feature ids of a registered collection, as the table store checks them, with each one's name."""
    from open_geo_service.features import store as feature_store
    from open_geo_service.features.services import registered_collections

    record = registered_collections().get(collection_id)
    if record is None or record.features is None:
        raise ValueError(f"feature collection '{collection_id}' is not registered; ingest it before this table")
    id_property = record.features.id_property
    rows = feature_store.read_feature_columns(record, list(dict.fromkeys([id_property, name_property])))
    if rows and name_property not in rows[0]:
        raise ValueError(f"feature collection '{collection_id}' has no property '{name_property}'")
    return {
        str(row[id_property]): (str(row[name_property]) if row.get(name_property) is not None else None)
        for row in rows
        if row.get(id_property) is not None
    }


def _instance_country() -> str | None:
    extent = get_extent()
    return str(extent.get("country_code")) if extent and extent.get("country_code") else None
