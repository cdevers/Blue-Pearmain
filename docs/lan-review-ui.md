# LAN Access for the Review UI

**Blue Pearmain — Feature Note**
*Status: Pending | Author: cdevers | Last updated: 2026-04-15*

---

## Goal

Make `bp ui` accessible from iOS devices (iPad, iPhone) on the local network, so photos can be reviewed without sitting at the Mac.

## Current State

The Flask server already binds to `0.0.0.0` by default (see `reviewer/app.py` → `main()`, `--host` default), so it accepts connections on all interfaces. The `bp` CLI doesn't forward `--host`, but `app.py`'s argparse default handles it.

The practical problem is **discoverability**: the Mac's LAN IP varies by network, so the correct URL (`http://192.168.x.x:5173`) is not obvious without checking `ifconfig` or System Settings each time.

## Planned Work

1. **Print the LAN URL on startup.** When `bp ui` starts, detect the machine's LAN IP and log it alongside the localhost URL:

   ```
   Starting review UI at http://localhost:5173
   Also available at  http://192.168.7.80:5173
   ```

   Use `socket.gethostbyname(socket.gethostname())` or iterate `socket.getaddrinfo` to find the first non-loopback IPv4 address.

2. **Optionally advertise via mDNS / Bonjour.** Register a `_http._tcp` service so the UI appears as `blue-pearmain.local:5173` on the LAN without needing to know the IP. This requires either the `zeroconf` Python package or a launchd helper. Lower priority.

3. **`--host` flag on `bp ui`.** The subparser already lacks `--host`; add it so users can override the bind address explicitly:

   ```
   bp ui --host 0.0.0.0 --port 5173
   ```

   And pass it through `cmd_ui` → `_inject_argv`.

## Non-Goals

- HTTPS / TLS termination (local network only, not needed)
- Authentication (local network, trusted devices)
- Remote (internet) access

## Workaround

Use `localhost` when reviewing from the Mac directly. LAN access works if you look up the IP manually and navigate to `http://<mac-ip>:5173`.
