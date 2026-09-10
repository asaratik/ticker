"""
Runs every query in the provisioned dashboards against a real database.

Dashboard JSON is the one place in this project where SQL isn't exercised by
anything else -- a typo would sit there until someone opened Grafana and saw
an empty panel. So these tests pull the SQL out of the checked-in JSON,
substitute what Grafana would substitute, and execute it.

They cover the SQL and the JSON structure, not Grafana itself: whether the
frser-sqlite-datasource plugin renders a given panel option is beyond what
can be checked here.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ticker.db import rollup, store
from ticker.ingest.session_logger import SessionLogger

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
PROVISIONING_DIR = Path(__file__).resolve().parent.parent / "grafana" / "provisioning"

UTC = timezone.utc
DATASOURCE_TYPE = "frser-sqlite-datasource"
DATASOURCE_UID = "ticker-sqlite"


def dashboards():
    found = sorted(DASHBOARD_DIR.glob("*.json"))
    assert found, "no dashboards found in {}".format(DASHBOARD_DIR)
    return found


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def targets(dashboard):
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            yield panel, target


def variable_defaults(dashboard):
    """What Grafana would substitute for each template variable."""
    return {
        variable["name"]: variable["current"]["value"]
        for variable in dashboard.get("templating", {}).get("list", [])
    }


def interpolate(sql, variables, from_ms, to_ms):
    """Do what Grafana does before the query reaches the datasource."""
    sql = sql.replace("$__from", str(from_ms)).replace("$__to", str(to_ms))
    for name, value in variables.items():
        sql = sql.replace("${" + name + "}", str(value))
        sql = re.sub(r"\$" + name + r"\b", str(value), sql)
    return sql


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """A database with a session's worth of data and rollups built."""
    db_path = tmp_path_factory.mktemp("dash") / "ticker.sqlite3"
    logger = SessionLogger("ble", db_path=db_path)
    base = datetime.now(UTC) - timedelta(minutes=30)
    try:
        logger.start_session(label="ride", device_name="HRM 600",
                             device_address="EC:1C")
        offset = 0.0
        for i in range(60):
            rr = 810.0 if i % 2 else 790.0
            offset += rr
            ts = base + timedelta(milliseconds=offset)
            logger.log_sample({
                "type": "sample",
                "timestamp": ts.isoformat(timespec="milliseconds"),
                "hr": 70 + (i % 20),
                "rr_intervals_ms": [rr],
            })
    finally:
        logger.close()

    conn = store.connect(db_path)
    rollup.rebuild_all(conn)
    conn.close()
    return db_path


# -- structure -----------------------------------------------------------

@pytest.mark.parametrize("path", dashboards(), ids=lambda p: p.name)
def test_dashboard_is_valid_json_with_the_required_fields(path):
    dashboard = load(path)
    for field in ("uid", "title", "panels", "schemaVersion"):
        assert field in dashboard, "{} is missing {!r}".format(path.name, field)
    assert dashboard["panels"], "no panels in " + path.name


def test_dashboard_uids_and_titles_are_unique():
    loaded = [load(path) for path in dashboards()]
    uids = [d["uid"] for d in loaded]
    titles = [d["title"] for d in loaded]
    assert len(set(uids)) == len(uids)
    assert len(set(titles)) == len(titles)


@pytest.mark.parametrize("path", dashboards(), ids=lambda p: p.name)
def test_panel_ids_are_unique_within_a_dashboard(path):
    ids = [panel["id"] for panel in load(path)["panels"]]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("path", dashboards(), ids=lambda p: p.name)
def test_every_target_points_at_the_provisioned_datasource(path):
    dashboard = load(path)
    for panel, target in targets(dashboard):
        source = target.get("datasource") or panel.get("datasource")
        assert source["uid"] == DATASOURCE_UID, panel["title"]
        assert source["type"] == DATASOURCE_TYPE, panel["title"]


@pytest.mark.parametrize("path", dashboards(), ids=lambda p: p.name)
def test_query_text_and_raw_query_text_agree(path):
    """The plugin executes one and shows the other in the editor. If they
    drift, the panel runs SQL that isn't what the editor displays."""
    for panel, target in targets(load(path)):
        assert target["queryText"] == target["rawQueryText"], panel["title"]


@pytest.mark.parametrize("path", dashboards(), ids=lambda p: p.name)
def test_time_series_panels_declare_a_time_column(path):
    for panel, target in targets(load(path)):
        if target.get("queryType") == "time series":
            assert target.get("timeColumns") == ["time"], panel["title"]
            assert " AS time" in target["queryText"], panel["title"]


# -- the SQL actually runs ----------------------------------------------

def all_targets():
    out = []
    for path in dashboards():
        dashboard = load(path)
        for panel, target in targets(dashboard):
            out.append(pytest.param(
                path.name, dashboard, panel, target,
                id="{}:{}".format(path.stem, panel["title"])))
    return out


@pytest.mark.parametrize("name,dashboard,panel,target", all_targets())
def test_every_panel_query_runs(name, dashboard, panel, target, populated):
    import sqlite3

    now = datetime.now(UTC)
    from_ms = int((now - timedelta(days=90)).timestamp() * 1000)
    to_ms = int((now + timedelta(days=1)).timestamp() * 1000)
    sql = interpolate(target["queryText"], variable_defaults(dashboard),
                      from_ms, to_ms)
    assert "$" not in sql, "unsubstituted variable in {}: {}".format(
        panel["title"], sql)

    conn = sqlite3.connect(str(populated))
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        pytest.fail("{} / {} failed: {}\n{}".format(name, panel["title"], exc, sql))
    finally:
        conn.close()

    if target.get("queryType") == "time series" and rows:
        # Grafana reads the time column as epoch milliseconds; seconds would
        # silently place every point in January 1970.
        stamp = rows[0][0]
        assert isinstance(stamp, int), panel["title"]
        assert stamp > 1_000_000_000_000, panel["title"]


@pytest.mark.parametrize("name,dashboard,panel,target", all_targets())
def test_every_panel_query_returns_data_for_a_populated_database(
        name, dashboard, panel, target, populated):
    """A query that parses but matches nothing is the failure mode this whole
    file exists to catch -- a panel that is silently always empty."""
    import sqlite3

    now = datetime.now(UTC)
    from_ms = int((now - timedelta(days=90)).timestamp() * 1000)
    to_ms = int((now + timedelta(days=1)).timestamp() * 1000)
    sql = interpolate(target["queryText"], variable_defaults(dashboard),
                      from_ms, to_ms)

    conn = sqlite3.connect(str(populated))
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    assert rows, "{} / {} returned nothing".format(name, panel["title"])


def test_the_resolution_variable_really_switches_tables(populated):
    """Section 9.1's rule -- rollups for long spans, raw for session detail --
    is enforced by a variable, so both settings have to work."""
    import sqlite3

    dashboard = load(DASHBOARD_DIR / "ticker-overview.json")
    panel = next(p for p in dashboard["panels"] if p["title"].startswith("Heart rate"))
    now = datetime.now(UTC)
    from_ms = int((now - timedelta(days=90)).timestamp() * 1000)
    to_ms = int((now + timedelta(days=1)).timestamp() * 1000)

    counts = {}
    conn = sqlite3.connect(str(populated))
    try:
        for resolution in ("daily", "raw"):
            sql = interpolate(panel["targets"][0]["queryText"],
                              {"res": resolution}, from_ms, to_ms)
            counts[resolution] = len(conn.execute(sql).fetchall())
    finally:
        conn.close()

    assert counts["daily"] >= 1
    # Raw is one row per sample; daily is one row per day. If they matched,
    # the variable isn't reaching the query.
    assert counts["raw"] > counts["daily"]


# -- provisioning --------------------------------------------------------

def test_provisioning_files_exist():
    assert (PROVISIONING_DIR / "datasources" / "ticker.yaml").exists()
    assert (PROVISIONING_DIR / "dashboards" / "ticker.yaml").exists()


def test_the_datasource_uid_matches_what_the_dashboards_reference():
    text = (PROVISIONING_DIR / "datasources" / "ticker.yaml").read_text(encoding="utf-8")
    assert "uid: {}".format(DATASOURCE_UID) in text
    assert "type: {}".format(DATASOURCE_TYPE) in text


def test_the_dashboard_provider_path_matches_the_compose_mount():
    provider = (PROVISIONING_DIR / "dashboards" / "ticker.yaml").read_text(encoding="utf-8")
    compose = (PROVISIONING_DIR.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/etc/grafana/dashboards" in provider
    assert "/etc/grafana/dashboards" in compose
