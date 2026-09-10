//
// The watch screen: current bpm, whether the last push was accepted, and
// where it's being sent. Deliberately plain -- this app exists to be
// glanced at when something isn't working.
//
using Toybox.Graphics;
using Toybox.Lang;
using Toybox.WatchUi;

class TickerBridgeView extends WatchUi.View {

    function initialize() {
        View.initialize();
    }

    function onUpdate(dc) {
        var app = getApp();

        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        var width = dc.getWidth();
        var height = dc.getHeight();

        // bpm, big, in the middle
        var bpm = (app.heartRate == null) ? "--" : app.heartRate.toString();
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(width / 2, height / 2, Graphics.FONT_NUMBER_HOT, bpm,
                    Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);

        // status, colour-coded, above it
        dc.setColor(_statusColor(app), Graphics.COLOR_TRANSPARENT);
        dc.drawText(width / 2, height * 0.22, Graphics.FONT_TINY, _statusText(app),
                    Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);

        // where it's going, below
        dc.setColor(Graphics.COLOR_LT_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.drawText(width / 2, height * 0.78, Graphics.FONT_XTINY, _host(app.endpoint),
                    Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
    }

    private function _statusText(app) {
        if (app.endpoint == null || app.endpoint.equals("")) {
            return "Set the URL in settings";
        }
        if (app.lastCode == null) {
            return "Connecting…";
        }
        if (app.lastCode == 200) {
            return "Sent " + app.sent.toString();
        }
        if (app.lastCode == 401) {
            return "Token rejected";
        }
        // Negative codes are Connect IQ's own (-104 = no phone/network,
        // -403 = the watch refused the URL), positive ones are HTTP.
        return "Error " + app.lastCode.toString();
    }

    private function _statusColor(app) {
        if (app.lastCode == 200) {
            return Graphics.COLOR_GREEN;
        }
        if (app.lastCode == null) {
            return Graphics.COLOR_YELLOW;
        }
        return Graphics.COLOR_RED;
    }

    // Just the host:port -- a full URL doesn't fit on a watch face and the
    // interesting part when something's wrong is which machine it's aimed at.
    private function _host(endpoint) {
        if (endpoint == null || endpoint.equals("")) {
            return "no endpoint";
        }
        var text = endpoint;
        var scheme = text.find("://");
        if (scheme != null) {
            text = text.substring(scheme + 3, text.length());
        }
        var path = text.find("/");
        if (path != null) {
            text = text.substring(0, path);
        }
        return text;
    }
}
