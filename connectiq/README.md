# Ticker Bridge (Connect IQ watch app)

Pushes the watch's heart rate to Ticker over HTTP, so the machine running
Ticker needs no Bluetooth or ANT+ hardware of its own. Pair it with
`HRM_SOURCE=http` on the PC side.

**Untested on real hardware.** It's written against the documented Connect
IQ APIs but has never been compiled or run here — there's no Garmin SDK or
watch on the machine it was written on. Expect to fix something the first
time you build it, and see "If it doesn't work" below for the parts most
likely to need it.

## How the pieces fit

```
Watch (Ticker Bridge)
  │  Communications.makeWebRequest, over the phone's connection or the
  │  watch's own Wi-Fi
  ▼
GET http://<pc-ip>:8476/hr?hr=142&device=Forerunner%20965
  │
  ▼
Ticker (HRM_SOURCE=http)  →  live bpm + SQLite session log
```

The watch still uses Bluetooth to talk to *its phone* — that's how a watch
works. The point is that nothing pairs with, plugs into, or is scanned for
by the PC: it just listens on a port.

Plain HTTP is deliberate. Connect IQ requires HTTPS for the public internet
but permits plain HTTP to LAN addresses, which is exactly what this is.

## Building and sideloading

1. Install the [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/)
   and the **Monkey C** extension for VS Code.
2. Open this folder (`connectiq/TickerBridge`) in VS Code.
3. Add your watch: **Monkey C: Edit Products**. The `<iq:products>` list in
   `manifest.xml` is a starting guess — if your model isn't in it, the build
   won't produce anything that runs on it.
4. **Monkey C: Build for Device** → produces `TickerBridge.prg`.
5. Plug the watch in over USB and copy the `.prg` into `GARMIN/APPS/` on it.
6. Unplug. The app appears in the watch's app list.

To try it without a watch, **Monkey C: Run** starts the simulator; in the
simulator turn off *Settings → Use Device HTTPS Requirements* so plain HTTP
to your PC is allowed.

## Settings

Set in Garmin Connect Mobile: **Connect IQ Apps → Ticker Bridge → Settings**.

| Setting | What to put in it |
|---|---|
| Ticker URL | The URL Ticker prints on its status line, e.g. `http://192.168.1.10:8476/hr` |
| Shared token | Only if you set `HRM_HTTP_TOKEN` on the PC; leave empty otherwise |
| Seconds between sends | `2` is a good default; `1` matches a chest strap's rate and costs more battery |
| Name to report | Recorded against the session in Ticker's database |

## What the watch screen shows

Current bpm in the middle, and above it the state of the last push:

| Shown | Means |
|---|---|
| `Connecting…` (yellow) | No response yet — nothing has been sent, or the first one is in flight |
| `Sent 47` (green) | 47 readings accepted |
| `Token rejected` (red) | The token doesn't match `HRM_HTTP_TOKEN` |
| `Error -104` (red) | Connect IQ's own code: no phone or no network |
| `Error -403` (red) | The watch refused the URL, usually the HTTPS rule |
| `Error 404` (red) | Reached a server, wrong path — the URL must end in `/hr` |

## If it doesn't work

Check reachability first, from a browser on any machine on the same network
as the watch: `http://<pc-ip>:8476/health` should answer `{"ok": true}`. If
that fails, the watch was never going to get through either, and the problem
is on the PC (firewall, wrong IP) rather than in this app.

- **Nothing arrives, `Error -104`.** The watch has no route to the network:
  phone out of range, Bluetooth off, or the watch's Wi-Fi isn't connected.
- **Nothing arrives, `/health` works from a browser.** Almost certainly the
  Windows Firewall prompt on the first run of Ticker was dismissed. Allow
  Python on private networks, or add a rule for TCP 8476.
- **`Error -403`.** The watch is enforcing HTTPS. Confirm the URL is an IP
  address on your LAN (`192.168.x.x`, `10.x.x.x`) and not a hostname that
  resolves somewhere public.
- **Build fails on `Application.Properties`.** It needs API level 3.1+. On
  an older watch, use `getApp().getProperty("endpoint")` instead.
- **Build fails on the products list.** Your device id isn't in
  `manifest.xml` — add it with **Monkey C: Edit Products**.
