# Windrose Control Panel

A small self-hosted web panel for Windrose dedicated servers.

It is designed for community servers that run Windrose+ and optionally WindroseRCON. The panel keeps host metrics, server controls, backups, config editing, Windrose+ commands, logs, and player actions in one place.

## Features

- Live Windrose status from Windrose+
- CPU, memory, disk, process, and service state
- Start, stop, and restart the Windrose systemd service
- State-aware service controls that disable invalid actions like starting an already-running server
- Edit `ServerDescription.json` values such as name, max players, and password
- Create tar.gz backups of save data and server config
- Run Windrose+ `wp.*` commands through the Windrose+ command spool
- Use WindroseRCON for `showplayers`, `kick`, `ban`, and `banlist` when the DLL is installed and listening
- Fill missing Account IDs from recent `R5.log` account records when WindroseRCON returns a player name without an ID
- Password-protected web UI

## Requirements

- Linux server with Python 3
- A Windrose server managed by systemd
- Windrose+ installed and writing `windrose_plus_data/server_status.json`
- Optional: WindroseRCON on `127.0.0.1:27065`

The default paths match an ARM Oracle Cloud setup:

```bash
/opt/windrose-direct/server
/etc/systemd/system/windrose.service
/etc/systemd/system/windrose-plus-dashboard.service
```

Override them in `/etc/windrose-panel.env` if your server layout is different.

## Install

```bash
git clone https://github.com/falcononrails/windrose-control-panel.git
cd windrose-control-panel
sudo ./install.sh
```

Open:

```text
http://YOUR_SERVER_IP:8790
```

The installer prints the generated password and stores it in:

```bash
/etc/windrose-panel.env
```

## Configuration

Example environment file:

```bash
PANEL_HOST=0.0.0.0
PANEL_PORT=8790
PANEL_PASSWORD=change-me
PANEL_SECRET=random-secret
WINDROSE_GAME_DIR=/opt/windrose-direct/server
WINDROSE_BACKUP_DIR=/opt/windrose-backups
WINDROSE_SERVICE=windrose.service
WINDROSE_PLUS_SERVICE=windrose-plus-dashboard.service
SOURCE_RCON_HOST=127.0.0.1
SOURCE_RCON_PORT=27065
```

Restart after edits:

```bash
sudo systemctl restart windrose-panel
```

## WindroseRCON Notes

WindroseRCON is the current open-source path for native kick and ban commands. The panel detects it automatically when:

- `windrosercon/settings.ini` exists under the Windrose `Win64` directory
- the RCON TCP port is listening locally

If it is unavailable, kick and ban buttons are disabled while Windrose+ commands, metrics, config editing, backups, and logs continue to work.

On ARM/Hangover, `dkoz/WindroseRCON` v1.0.2 has been tested with this panel. v1.0.3 crashed during startup on the Oracle ARM test host, so pin v1.0.2 unless a newer release confirms ARM/Hangover compatibility.

Some Windrose builds expose player names through WindroseRCON before they expose Account IDs. The panel falls back to recent game log account records to populate the Account ID column for live player rows.

## Security

Keep the panel behind a strong password. If you expose it to the internet, restrict the firewall source IPs when possible.

The panel intentionally talks to WindroseRCON on `127.0.0.1` by default, so the RCON port does not need to be public.
