"""
Data model — Phase 1 (Role B).

This module has two jobs:
  1. Load the 8 instance CSVs into pandas DataFrames (done below — boring
     plumbing, no reason for both of us to spend time on it).
  2. Resolve, for any activity, the ordered list of location_ids (tunnel
     sectors + platform sectors) it books from start to end ("book-in" to
     "book-out").

Confirmed from the real data (not just the README) before writing this:
  - `start_location_id` / `end_location_id` in 08_ACTIVITY_DETAILS.csv are
    already full location_ids of *tunnel sectors*, e.g. "SEC:BET:S15_S16:EB"
    — not bare station ids. So there is no separate "which line/bound is
    this activity on" lookup needed: it's encoded in the id itself.
  - A sector's `seq` (in 03_SECTORS.csv) gives its position along the line,
    ignoring bound. EB and WB share the same sequence of sectors — only the
    location_id's `:EB`/`:WB` suffix differs.
  - An activity can run in either direction (start seq > end seq is valid —
    see e.g. A012: SEC:BET:H01_H02:WB -> SEC:BET:H02_S15:WB is seq 14 -> 15,
    forward; but plenty of others run the other way).
  - A same-start-and-end activity (e.g. A007: SEC:BET:H01_H02:EB twice) is a
    single-sector job — confirmed against 03_submission_sample: it books
    BOTH platforms on either end of that one sector, plus the sector itself.
"""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "01_data"


@dataclass
class Instance:
    lines: pd.DataFrame
    stations: pd.DataFrame
    sectors: pd.DataFrame
    location_supply: pd.DataFrame
    buffer_location: pd.DataFrame
    parameters: dict
    contracts: pd.DataFrame
    activities: pd.DataFrame


def load_instance(data_dir: Path = DATA_DIR) -> Instance:
    """Load all 8 instance CSVs. Pure I/O, no validation — smoke_test.py owns that."""
    params_df = pd.read_csv(data_dir / "06_PARAMETERS.csv")
    return Instance(
        lines=pd.read_csv(data_dir / "01_LINES.csv"),
        stations=pd.read_csv(data_dir / "02_STATIONS.csv"),
        sectors=pd.read_csv(data_dir / "03_SECTORS.csv"),
        location_supply=pd.read_csv(data_dir / "04_LOCATION_SUPPLY.csv"),
        buffer_location=pd.read_csv(data_dir / "05_BUFFER_LOCATION.csv"),
        parameters=dict(zip(params_df["key"], params_df["value"])),
        contracts=pd.read_csv(data_dir / "07_PROJECT_DETAILS.csv"),
        activities=pd.read_csv(data_dir / "08_ACTIVITY_DETAILS.csv"),
    )


def parse_location_id(location_id: str) -> dict:
    """
    Split a sector location_id into its parts.

    "SEC:BET:S15_S16:EB" -> {"kind": "SEC", "line": "BET",
                              "from": "S15", "to": "S16", "bound": "EB"}

    Every activity's start/end location_id has this shape (confirmed: always
    kind == "SEC", never "PLAT" — see smoke_test.py). Platform ids look like
    "PLAT:ALP:S03:EB" (one station, not two) and only ever appear as OUTPUT
    of the resolver below, never as activity input.
    """
    kind, line, span, bound = location_id.split(":")
    frm, to = span.split("_")
    return {"kind": kind, "line": line, "from": frm, "to": to, "bound": bound}


def resolve_activity_path(start_location_id: str, end_location_id: str, instance: Instance) -> list[str]:
    """
    Given an activity's start and end tunnel-sector location_ids, return the
    ORDERED list of every location_id it books — both tunnel sectors AND
    platform sectors — from book-in to book-out.

    Verified against every one of the 54 real activities (not just A001/A007)
    by diffing against 03_submission_sample/SCHEDULE_OCCUPANCY.csv — see
    tests/test_path_resolver.py. All 54 match exactly, both as a location
    SET and in this function's chosen order.

    Review points (judgment calls, not derived facts):
      - Asserts start/end share the same line and bound — true for all 54
        real activities, but nothing in the schema *forces* it. If a hidden
        judging instance ever has a cross-line/cross-bound activity, this
        will raise loudly rather than silently mis-schedule it.
      - Chosen output order is platform-first, alternating
        (platform, tunnel, platform, tunnel, ..., platform). The sample
        submission happens to order it this way too, but the schema doesn't
        require any particular order — only the SET of booked locations
        matters to the validator.
    """
    start = parse_location_id(start_location_id)
    end = parse_location_id(end_location_id)

    assert start["line"] == end["line"], (
        f"start/end on different lines: {start_location_id} vs {end_location_id}"
    )
    assert start["bound"] == end["bound"], (
        f"start/end on different bounds: {start_location_id} vs {end_location_id}"
    )
    line = start["line"]
    bound = start["bound"]

    # A location_id's bare sector_id (as it appears in 03_SECTORS.csv) drops
    # the bound suffix, e.g. "SEC:BET:S15_S16:EB" -> "SEC:BET:S15_S16".
    def bare_sector_id(parts: dict) -> str:
        return f"{parts['kind']}:{parts['line']}:{parts['from']}_{parts['to']}"

    sectors_on_line = instance.sectors[instance.sectors["line_code"] == line].set_index("sector_id")
    seq_start = sectors_on_line.loc[bare_sector_id(start), "seq"]
    seq_end = sectors_on_line.loc[bare_sector_id(end), "seq"]

    # The activity may run in either direction (start seq > end seq is valid —
    # e.g. some activities run "backwards" along the line). We only care
    # about which sectors lie between the two endpoints, inclusive, so sort
    # by seq value rather than by which one is "start".
    low_seq, high_seq = sorted([seq_start, seq_end])
    path_sectors = (
        sectors_on_line[(sectors_on_line["seq"] >= low_seq) & (sectors_on_line["seq"] <= high_seq)]
        .sort_values("seq")
        .reset_index()  # bring sector_id back as a column
    )

    # Walk the chain of sectors and interleave platform / tunnel bookings:
    # platform at the first station, then (tunnel, platform) for every
    # sector along the path. Consecutive sectors always chain
    # (sector[i].to_station_id == sector[i+1].from_station_id) because
    # 03_SECTORS.csv is a simple ordered line per line_code.
    path: list[str] = [f"PLAT:{line}:{path_sectors.iloc[0]['from_station_id']}:{bound}"]
    for _, row in path_sectors.iterrows():
        path.append(f"{row['sector_id']}:{bound}")
        path.append(f"PLAT:{line}:{row['to_station_id']}:{bound}")

    return path


if __name__ == "__main__":
    inst = load_instance()
    print(f"Loaded instance: {len(inst.activities)} activities, {len(inst.contracts)} contracts, "
          f"horizon {inst.parameters['horizon_weeks']} weeks from {inst.parameters['horizon_start']}")