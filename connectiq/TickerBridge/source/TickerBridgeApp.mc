//
// Ticker Bridge -- pushes the watch's heart rate to a Ticker instance over
// HTTP, so the PC running Ticker needs no Bluetooth or ANT+ hardware of its
// own.
//
// The watch reaches the PC over whatever network path it already has: the
// paired phone's connection, or the watch's own Wi-Fi. Plain HTTP is fine
// here because Connect IQ allows it for LAN addresses (it requires HTTPS
// only for the public internet).
//
using Toybox.Application;
using Toybox.Communications;
using Toybox.Lang;
using Toybox.Sensor;
using Toybox.System;
using Toybox.Timer;
using Toybox.WatchUi;

class TickerBridgeApp extends Application.AppBase {

    // Shown by the view, so it can say what's actually happening rather
    // than just sitting there.
    var heartRate = null;      // last reading from the sensor
    var lastCode = null;       // last HTTP/Connect IQ response code
    var sent = 0;              // readings accepted by the server
    var endpoint = "";

    private var _timer = null;
    private var _inFlight = false;

    function initialize() {
        AppBase.initialize();
    }

    function onStart(state) {
        endpoint = _setting("endpoint", "");

        Sensor.setEnabledSensors([Sensor.SENSOR_HEARTRATE]);
        Sensor.enableSensorEvents(method(:onSensor));

        // One reading per second is what a strap sends; the default of 2s
        // halves the radio traffic (and the battery cost of it) for a live
        // number that still looks live.
        var seconds = _setting("intervalSec", 2);
        if (seconds < 1) { seconds = 1; }

        _timer = new Timer.Timer();
        _timer.start(method(:onTick), seconds * 1000, true);
    }

    function onStop(state) {
        if (_timer != null) {
            _timer.stop();
            _timer = null;
        }
        Sensor.enableSensorEvents(null);
        Sensor.setEnabledSensors([]);
    }

    function getInitialView() {
        return [new TickerBridgeView()];
    }

    // -- sensor ----------------------------------------------------------

    function onSensor(info) {
        heartRate = (info has :heartRate) ? info.heartRate : null;
        WatchUi.requestUpdate();
    }

    // -- sending ---------------------------------------------------------

    function onTick() {
        // Nothing to say yet: the optical sensor takes a few seconds to
        // settle after the app starts, and reads null until it does.
        if (heartRate == null) {
            return;
        }
        // Connect IQ allows one outstanding request at a time. On a slow
        // link, firing again would fail the new request rather than queue
        // it -- skipping this tick just drops one reading instead.
        if (_inFlight) {
            return;
        }
        if (endpoint == null || endpoint.equals("")) {
            lastCode = null;
            WatchUi.requestUpdate();
            return;
        }

        var params = {
            "hr" => heartRate,
            "device" => _setting("deviceName", "Garmin watch")
        };
        var token = _setting("token", "");
        if (!token.equals("")) {
            params.put("token", token);
        }

        var options = {
            :method => Communications.HTTP_REQUEST_METHOD_GET,
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };

        _inFlight = true;
        Communications.makeWebRequest(endpoint, params, options, method(:onResponse));
    }

    function onResponse(responseCode, data) {
        _inFlight = false;
        lastCode = responseCode;
        if (responseCode == 200) {
            sent = sent + 1;
        }
        WatchUi.requestUpdate();
    }

    // -- settings --------------------------------------------------------

    // Properties.getValue throws rather than returning null for a key that
    // isn't there, which happens on a fresh sideload before Garmin Connect
    // has pushed the defaults down.
    private function _setting(key, fallback) {
        var value = null;
        try {
            value = Application.Properties.getValue(key);
        } catch (ex) {
            value = null;
        }
        return (value == null) ? fallback : value;
    }
}

function getApp() {
    return Application.getApp();
}
