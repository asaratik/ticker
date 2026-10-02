"""Bounded, read-only data for native views; no Qt or HTTP dependency."""

from datetime import timedelta
from pathlib import Path

from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.tools import Tools
from ticker.model import iso_utc, now_utc, parse_iso

LABELS = {
    "heart_rate_bpm": "Heart rate", "resting_heart_rate_bpm": "Resting heart rate",
    "hrv_rmssd_ms": "Heart-rate variability", "sleep_duration_s": "Sleep duration",
}


def metric_label(name):
    return LABELS.get(name, name.replace("_", " ").capitalize())


def display_value(value, unit):
    if value is None:
        return "—"
    if unit == "s":
        minutes = round(value / 60)
        return "{}h {}m".format(minutes // 60, minutes % 60)
    return "{:g} {}".format(round(value, 1), unit or "").strip()


def chart_points(payload):
    """Convert bounded tool rows to coordinates, retaining real time gaps."""
    result = []
    for row in payload.get("rows", []):
        if len(row) < 2 or not isinstance(row[1], (int, float)):
            continue
        try:
            instant = parse_iso(row[0])
        except (ValueError, TypeError):
            continue
        result.append((instant.timestamp(), float(row[1])))
    return result


class Reader:
    def __init__(self, path: Path):
        self.db = ReadOnlyDatabase(path)
        self.tools = Tools(self.db)

    def close(self):
        self.db.close()

    def latest(self):
        """One most recent observation per summary metric, with provenance.

        A latest observation is not necessarily today's observation. The
        native view always displays its timestamp and reporting source.
        """
        rows = []
        with self.db.session() as conn:
            for name in ("resting_heart_rate_bpm", "hrv_rmssd_ms", "sleep_duration_s"):
                row = conn.execute(
                    "SELECT o.value, o.ts, s.display_name, m.unit FROM observations o "
                    "JOIN metrics m ON m.id = o.metric_id "
                    "JOIN sources s ON s.id = o.source_id "
                    "WHERE m.name = ? AND o.value IS NOT NULL "
                    "ORDER BY o.ts DESC, o.id DESC LIMIT 1", (name,)).fetchone()
                rows.append({"metric": name, "label": metric_label(name),
                             "value": row[0] if row else None,
                             "timestamp": row[1] if row else None,
                             "source": row[2] if row else None,
                             "unit": row[3] if row else None})
        return rows

    def sources(self, metric):
        with self.db.session() as conn:
            return [{"id": sid, "name": name} for sid, name in conn.execute(
                "SELECT DISTINCT s.id, s.display_name FROM sources s "
                "JOIN observations o ON o.source_id = s.id "
                "JOIN metrics m ON m.id = o.metric_id "
                "WHERE m.name = ? ORDER BY s.id", (metric,))]

    def series(self, metric, source_id, days=1):
        if source_id is None:
            return {"rows": [], "unit": "", "source": "Choose a source"}
        end = now_utc()
        start = end.astimezone(self.tools.zone).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        return self.tools.call("get_timeseries", {
            "metric": metric, "source_id": source_id, "start": iso_utc(start),
            "end": iso_utc(end), "max_points": 300})

    def sessions(self):
        return self.tools.call("list_sessions", {
            "start": "1970-01-01T00:00:00Z", "limit": 50})

    def session(self, session_id):
        return self.tools.call("get_session", {"session_id": session_id,
                                               "max_points": 300})
