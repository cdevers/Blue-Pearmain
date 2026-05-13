# Daemon setup

Blue Pearmain runs three background services as launchd agents — no terminal window required once they're loaded.

| Agent | Schedule | What it does |
|---|---|---|
| `com.blue-pearmain.poller` | Hourly | Polls Flickr for new uploads; auto-pushes approved photos |
| `com.blue-pearmain.pipeline` | Every 6 hours | Diffs metadata caches; auto-applies non-conflict proposals |
| `com.blue-pearmain.reviewer` | Always on | Serves the review UI; restarts automatically on crash |

## Installing

```bash
mkdir -p ~/Library/Logs/BluePearmain
bp install-daemons
```

`bp install-daemons` writes the three plists to `~/Library/LaunchAgents/` with all paths substituted for your install location. The output shows the exact `launchctl bootstrap` commands to load each one:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.poller.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.reviewer.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.pipeline.plist
```

## Logs

Logs are written to `~/Library/Logs/BluePearmain/` and are also visible in Console.app.

```bash
tail -f ~/Library/Logs/BluePearmain/reviewer.log
tail -f ~/Library/Logs/BluePearmain/poller.log
tail -f ~/Library/Logs/BluePearmain/pipeline.log
```

## Restarting a service

```bash
launchctl stop com.blue-pearmain.reviewer
launchctl start com.blue-pearmain.reviewer
```

If you get "Input/output error" from launchctl (stale state after an unclean stop), use `bootout`/`bootstrap` instead of `stop`/`start`:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.reviewer.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.reviewer.plist
```

## Uninstalling

```bash
bp uninstall-daemons
# Then bootout any agents that were loaded:
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.poller.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.reviewer.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.pipeline.plist
```

`bp uninstall-daemons --dry-run` previews what would be removed without deleting anything.
