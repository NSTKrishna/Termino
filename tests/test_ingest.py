"""Ingestion: idempotency, cross-source equivalence, sanitising, rejection."""

from __future__ import annotations

import pandas as pd
import pytest

from tbfmon import db
from tbfmon.ingest import ingest
from tbfmon.simulate import generate
from tbfmon.sources import (
    CsvSource, GroundTruthRefused, ModbusSource, MqttSource, SimulatorSource, Source,
)


@pytest.fixture(scope="module")
def dataset():
    return generate(n_fans=12, years=2, seed=11)


@pytest.fixture
def bundle(tmp_path, dataset):
    dataset.write(tmp_path / "sim", tmp_path / "ground_truth")
    return tmp_path / "sim"


def test_csv_ingest_is_idempotent(conn, bundle):
    first = ingest(conn, CsvSource(bundle))
    counts = db.counts(conn)
    assert first.total("inserted") > 0 and counts["test_run"] > 0
    second = ingest(conn, CsvSource(bundle))
    assert second.total("inserted") == 0 and second.total("updated") == 0
    assert second.total("unchanged") == first.total("inserted") + first.total("unchanged")
    assert db.counts(conn) == counts
    assert conn.execute("SELECT COUNT(*) FROM ingest_batch").fetchone()[0] == 2


def test_simulator_then_csv_is_noop(conn, dataset, bundle):
    ingest(conn, SimulatorSource(dataset))
    counts = db.counts(conn)
    again = ingest(conn, CsvSource(bundle))
    assert again.total("inserted") == 0 and again.total("updated") == 0
    assert db.counts(conn) == counts


def test_duplicate_rows_collapse(conn, dataset, bundle):
    rep = ingest(conn, CsvSource(bundle))
    dups = int(dataset.test_runs[dataset.test_runs.fan_id.isin(dataset.fans.fan_id)].duplicated().sum())
    assert rep.by_kind["test_run"]["unchanged"] >= dups


def test_changed_row_is_updated(conn, bundle):
    ingest(conn, CsvSource(bundle))
    runs = pd.read_csv(bundle / "test_runs.csv", dtype=str)
    runs.loc[0, "duration_min"] = "99.9"
    runs.to_csv(bundle / "test_runs.csv", index=False)
    rep = ingest(conn, CsvSource(bundle / "test_runs.csv"))
    assert rep.by_kind["test_run"]["updated"] == 1


def test_sentinels_and_range_violations_nulled_not_rejected(conn, bundle):
    ingest(conn, CsvSource(bundle / "fans.csv"))
    fan_id = pd.read_csv(bundle / "fans.csv").fan_id[0]
    csv = bundle.parent / "manual.csv"
    csv.write_text(
        "fan_id,timestamp,run_type,duration_min,vib_de_rms_mms,temp_bearing_de_c,rpm,noise_dba\n"
        f"{fan_id},2030-01-01T02:00:00,scheduled_test,15,-999,45.2,abc,12\n"
    )
    rep = ingest(conn, CsvSource(csv, kind="test_run"))
    assert rep.by_kind["test_run"]["inserted"] == 1 and rep.values_nulled == 3
    row = db.runs_df(conn, fan_id).iloc[-1]
    assert pd.isna(row.vib_de_rms_mms) and pd.isna(row.rpm) and pd.isna(row.noise_dba)
    assert row.temp_bearing_de_c == 45.2
    issues = {(i["field"], i["issue"], i["action"]) for i in db.issues(conn)}
    assert ("vib_de_rms_mms", "sentinel_value", "nulled") in issues
    assert ("rpm", "unparseable", "nulled") in issues
    assert ("noise_dba", "out_of_sensor_range", "nulled") in issues


def test_unknown_fan_and_schema_errors_rejected_and_logged(conn, bundle):
    with (bundle / "test_runs.csv").open("a") as f:
        f.write("TBF-O01,2024-02-02T01:00:00,scheduled_test,10" + "," * 23 + "\n")  # transcription typo
    rep = ingest(conn, CsvSource(bundle))
    assert rep.by_kind["test_run"]["rejected"] >= 1
    rejected = [i for i in db.issues(conn) if i["action"] == "rejected"]
    assert any(i["issue"] == "unknown_fan_id" for i in rejected)
    n_issues = len(db.issues(conn))
    ingest(conn, CsvSource(bundle))
    assert len(db.issues(conn)) == n_issues  # issue log is idempotent too


def test_bad_enum_rejected(conn, bundle):
    ingest(conn, CsvSource(bundle / "fans.csv"))
    fan_id = pd.read_csv(bundle / "fans.csv").fan_id[0]
    csv = bundle.parent / "m.csv"
    csv.write_text(f"fan_id,date,task,type\n{fan_id},2024-01-01,Inspect,sometimes\n")
    rep = ingest(conn, CsvSource(csv, kind="maintenance"))
    assert rep.by_kind["maintenance"]["rejected"] == 1
    assert rep.rejected_examples[0]["field"] == "type"


def test_ground_truth_refused(tmp_path, dataset):
    out = dataset.write(tmp_path / "sim", tmp_path / "ground_truth")
    with pytest.raises(GroundTruthRefused):
        CsvSource(out["ground_truth"], kind="failure")
    with pytest.raises(GroundTruthRefused):
        CsvSource(tmp_path / "ground_truth")


def test_unknown_single_file_needs_kind(tmp_path):
    f = tmp_path / "readings.csv"
    f.write_text("a\n1\n")
    with pytest.raises(ValueError, match="--kind"):
        CsvSource(f)


def test_sources_conform_and_stubs_raise(dataset, bundle):
    for src in (CsvSource(bundle), SimulatorSource(dataset), MqttSource(), ModbusSource()):
        assert isinstance(src, Source)
    for stub in (MqttSource(), ModbusSource()):
        with pytest.raises(NotImplementedError):
            list(stub.records())
