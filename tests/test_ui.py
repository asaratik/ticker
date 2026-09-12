from html.parser import HTMLParser

from ticker.app.web import STATIC_DIR


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        item = dict(attrs).get("id")
        if item:
            self.ids.add(item)


def test_first_run_backup_and_history_controls_are_present():
    parser = Ids()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    assert {"start-progress", "pick-import", "trend-metric", "trend",
            "backup", "check-update", "diagnostics"} <= parser.ids


def test_oura_uses_oauth_and_no_retired_personal_token_field():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "oura-client-id" in html and "oura-client-secret" in html
    assert "127.0.0.1:8478/callback" in html
    assert "oura-token" not in html + script


def test_support_download_is_generated_locally_from_redacted_json():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'call("GET", "/ui/diagnostics")' in script
    assert "ticker-diagnostics.json" in script
