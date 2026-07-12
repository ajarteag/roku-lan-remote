# Roku LAN Remote

A self-hosted web remote + live status dashboard + automation runner for Roku
TVs, plus a terminal (TUI) remote. Runs on any computer on the same network as
the TV. **Zero dependencies** — Python 3 standard library only. Talks to the
TV via Roku's built-in [External Control Protocol](https://developer.roku.com/dev/docs/external-control-api)
(ECP, port 8060), the same API the official mobile app uses.

- 📺 **Live status** — power state, active app + icon, play/pause, position
- 🎛️ **Full remote** — d-pad, playback, volume, power; hardware-keyboard
  support on desktop; works *inside* apps (YouTube, Netflix, …)
- 🚀 **App launcher** — every installed channel with its real icon, plus
  one-tap input switching (HDMI, AV, tuner)
- ⌨️ **Text sender** — type into TV search/login keyboards from a real keyboard
- ⚡ **Automations** — macro sequences (power on → wait → launch Netflix),
  editable in the UI, each triggerable via a plain HTTP call
- 🖥️ **TUI** — a curses terminal remote with the same superpowers, no server
  needed

## Prerequisites

- A Roku TV or player on your local network.
- On the TV: **Settings → System → Advanced system settings → Control by
  mobile apps → Network access → Default**. (No developer mode required —
  this is the only setting ECP needs. On "Limited", commands are rejected.)
- Python 3.9+ on the host computer (preinstalled on macOS; `python3 --version`
  to check). Nothing to `pip install`.
- Host and TV on the same LAN/subnet.

## Quick start

```sh
git clone https://github.com/ajarteag/roku-lan-remote.git
cd roku-lan-remote
python3 discover.py --save   # find your Roku and write its IP to config.json
python3 server.py            # serve the web remote on port 8000
```

Open `http://localhost:8000` on the host, or `http://<host-ip>:8000` from any
phone/laptop on the network. On iPhone, Share → **Add to Home Screen** gives
you a fullscreen, app-like remote.

If discovery fails (some Wi-Fi setups block multicast and scanning), find the
TV's IP under **Settings → Network → About** and put it in `config.json`.

### Desktop keyboard shortcuts (web app)

Arrows navigate · Enter = OK · Backspace = back · Esc = home ·
Space = play/pause.

## Terminal remote (TUI)

```sh
python3 roku_tui.py                  # uses tv_ip from config.json
python3 roku_tui.py --ip 192.168.1.50
```

Add an alias to `~/.zshrc` for one-word launch:
`alias tv='python3 ~/path/to/roku-lan-remote/roku_tui.py'`

Keys: arrows navigate · enter OK · delete back · `h` home · space play/pause ·
`<`/`>` rew/fwd · `r` replay · `i` options · `+`/`-` volume · `m` mute ·
`p` power · `a` app picker (type to filter) · `t` type mode (keystrokes go to
on-screen keyboards) · `q` quit.

## Automations

`macros.json` holds a list of `{name, icon, steps}` (editable in the web UI
under *Automations → Edit*). Steps run in order:

- `{"type": "keypress", "value": "PowerOn"}` — any ECP key
  (`Home`, `Select`, `VolumeUp`, `PowerOff`, …)
- `{"type": "launch", "value": "12"}` — app ID or TV input
  (`tvinput.hdmi1`, `tvinput.dtv`, …). `GET /api/apps` lists your IDs.
- `{"type": "delay", "value": 2000}` — milliseconds (max 30000)

Every macro is also an HTTP endpoint, so anything that can make a request can
trigger one — Apple Shortcuts, Siri, Raycast, cron, Home Assistant:

```sh
curl -X POST "http://<host-ip>:8000/api/macro/Movie%20Night"
```

## API

| Route | Description |
|---|---|
| `GET /api/status` | power, active app, play state, position |
| `GET /api/apps` | installed apps + inputs, with launch IDs |
| `POST /api/keypress/<key>` | press a remote key |
| `POST /api/launch/<id>` | launch an app or switch input |
| `POST /api/text` | body `{"text": "hello"}` — types on the TV |
| `GET/POST /api/macros` | read / replace the automation list |
| `POST /api/macro/<name>` | run one automation |

## Hosting it permanently (macOS example)

To keep it running on an always-on machine (Mac mini/Studio, a NAS, a Pi):

1. Clone the repo there and run `python3 discover.py --save` once.
2. Give the host a static IP or DHCP reservation in your router.
3. macOS — create `~/Library/LaunchAgents/com.roku.remote.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.roku.remote</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOUR_USER/roku-lan-remote/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

4. `launchctl load ~/Library/LaunchAgents/com.roku.remote.plist`

macOS will prompt once to allow Python to accept local network connections —
approve it. (Linux equivalent: a systemd unit running
`python3 /path/to/server.py`.)

## A memorable URL (`http://tv`)

Skip "what's the IP again?" by giving the host a friendly name in your
router's local DNS. On OpenWrt-based routers (including GL.iNet), dnsmasq
serves DNS to every client, so one entry covers all devices:

**GL.iNet (e.g. Beryl AX / GL-MT3000):**

1. Reserve the host's IP: admin panel → **Clients** → your host → *Modify* →
   fix/bind the IP (so the DNS entry never goes stale).
2. Open LuCI (admin panel → **System → Advanced Settings**), or SSH to the
   router, and add a dnsmasq address entry:

   ```sh
   # via SSH on the router
   uci add_list dhcp.@dnsmasq[0].address='/tv/192.168.4.10'
   uci commit dhcp && /etc/init.d/dnsmasq restart
   ```

   (In LuCI: **Network → DHCP and DNS → General → Addresses** → add
   `/tv/192.168.4.10`.)
3. To drop the `:8000` from the URL, serve on port 80: set
   `"server_port": 80` in `config.json` and run the server as root — on
   macOS put the plist in `/Library/LaunchDaemons/` instead (ports below
   1024 need root). Otherwise the URL is `http://tv:8000`.

Notes:
- The first time, type the full `http://tv` — some browsers treat
  bare made-up TLDs as search queries until they learn it's a site.
- Clients using hardcoded/encrypted DNS (iCloud Private Relay, DoH) may
  bypass router DNS for custom names; turning those off for your home
  Wi-Fi fixes it.
- Zero-router-config alternative on Apple networks: set the host's local
  hostname to `roku` (System Settings → General → Sharing) and use
  `http://roku.local:8000` via mDNS/Bonjour.

## Repo layout

- `server.py` — web server + ECP proxy (stdlib only)
- `static/` — the web UI
- `roku_tui.py` — standalone terminal remote
- `discover.py` — find Rokus on the LAN, `--save` writes `config.json`
- `config.json` — TV IP + server port
- `macros.json` — automation definitions

## How it works

Roku devices expose a plain HTTP API on port 8060 (ECP): `POST /keypress/<key>`
presses remote buttons (which is why navigation works inside any app),
`POST /launch/<id>` opens apps and inputs, and `GET /query/*` reports device,
app, and playback state. `server.py` proxies those endpoints, parses the XML
into JSON, and serves the single-page UI in `static/`. The TUI talks to the
TV directly.

Not possible by design: screenshots/video of protected app content (Roku only
allows screen capture for sideloaded dev channels), and there's no clean API
for arbitrary TV settings — only what's reachable via remote keys.
