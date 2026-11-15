# Transmission Seeds Cleaner

Two cooperating tools that keep a Plex + *arr + Transmission (+ cross-seed) box
from wasting disk space, without breaking private-tracker seeding rules:

| Script | What it does | When |
|---|---|---|
| `maintainerr_gate.py` | Holds back [Maintainerr](https://github.com/Maintainerr/Maintainerr) deletions while any torrent that shares the item's files still has to seed. | Daily, **before** Maintainerr's collection job |
| `torrent_checker.py` | Removes torrents that are no longer in the library once every torrent sharing their files has seeded long enough. Also removes torrents the tracker has deleted and re-verifies corrupt ones. | Daily, **after** Maintainerr's collection job |

The idea: a file only frees disk space when **every** hardlink to it is gone
(library copy, original download, every cross-seed copy). Deleting the Plex copy
while a torrent must keep seeding frees nothing and loses the title, and
deleting a torrent early breaks tracker rules. So both tools reason about
**groups of torrents that share files** and act only when the whole group is done.

## How the pieces fit together

```
03:50  maintainerr_gate.py   postpone due items whose torrent group still seeds
04:05  Maintainerr           deletes due items (Radarr/Sonarr + original torrent)
05:05  Maintainerr           re-evaluates rules, queues new items
06:30  torrent_checker.py    removes leftover torrent groups (e.g. cross-seeds)
```

1. **Maintainerr rules** decide *what* should go (see [Maintainerr rules](#maintainerr-rules)).
2. **The gate** decides *when*: an item due for deletion is postponed until the
   last torrent sharing its files has met the seeding threshold.
3. **Maintainerr** deletes the media and (via its download-client integration)
   the original torrent.
4. **The cleaner** removes the remaining cross-seed torrents of that group,
   which frees the space.

## Torrent groups

Torrents are grouped when they share any file on disk, either by hardlink
(cross-seed `linkType: hardlink`, *arr imports) or because two torrents point
at the very same path. Each torrent's files come from Transmission's own file
list; folders are never scanned, because several torrents can share one folder
name (cross-seed season packs all called `Season 1` end up in the same
`cross-seed/<tracker>/Season 1/` folder).

A torrent has **finished seeding** when it is complete and either public, or
private with `secondsSeeding >= seeding_time_threshold_days`. Transmission's
accumulated seeding time is used, not the time since it was added.

## `torrent_checker.py`: cleanup rules

For every group with at least one torrent inside `torrent_directories`:

| Situation | Decision | `flag_reason` |
|---|---|---|
| Any member's files are in the library (`check_directories`) | keep | `group_linked_to_library` |
| Any member has not finished seeding | keep, report blockers | `group_member_below_threshold` |
| Single torrent, files gone from disk | delete | `missing_files` |
| Single torrent, >2 seeds on tracker | delete | `not_hardlinked_with_more_than_two_seeds` |
| Single torrent, ≤2 seeds | keep (don't kill a rare release) | `two_or_fewer_seeds` |
| Several torrents, all finished seeding | delete the **whole group** | `cross_seed_group_all_seeded` |
| Tracker says the torrent is unregistered/deleted | delete that torrent | `tracker_unavailable` |

Data is deleted together with the torrent, **except** when a torrent that is
being kept points at the same path. Then only the torrent is removed
(`delete_data=False`). Removing one hardlink never affects the others.

```bash
.venv/bin/python torrent_checker.py            # report, then ask before deleting
.venv/bin/python torrent_checker.py --verbose  # also list kept torrents
.venv/bin/python torrent_checker.py --json     # machine-readable report, never deletes
.venv/bin/python torrent_checker.py --yes      # delete flagged torrents unattended (cron)
```

## `maintainerr_gate.py`: deletion gate

For every item in an active Maintainerr collection that is due within
`--lookahead-hours` (default 24):

1. Get the item's file paths from Maintainerr (`/api/media-server/meta/:id`, plus
   episodes for seasons/shows) and translate them with `path_mappings`.
2. Find every torrent that shares those files, and the full group of each.
3. If any member hasn't finished seeding, call Maintainerr's
   `POST /api/collections/media/postpone` so the item is deleted when the
   slowest member is done (`ceil(days left)`).
   - Paused/stopped torrents don't accumulate seeding time: re-checked every 7 days.
4. Otherwise do nothing; Maintainerr deletes it at its next collection run.

Fail-safe behaviour (an item is never deleted unchecked):

- None of the item's files exist on disk (wrong `path_mappings`?): postpone 1 day.
- Transmission is unreachable: every due item is postponed 1 day.
- Any error while checking an item: that item is postponed 1 day.

If the gate itself cannot run (e.g. Maintainerr's API is down), nothing is
postponed; check the log after any failed run.

```bash
.venv/bin/python maintainerr_gate.py --dry-run --all   # what would happen to every item
.venv/bin/python maintainerr_gate.py --dry-run         # only items due in the next 24h
.venv/bin/python maintainerr_gate.py                   # postpone for real (cron)
```

## Maintainerr rules

The gate does not decide *what* to delete; Maintainerr's rules do. The setup
this was built for only cleans up **requested** content (anything added
manually is kept):

```
requested (Radarr/Sonarr tag "request", or "Requested in Seerr")
AND (
      the requester watched it         movie: "Viewed by" contains the requester
                                       season: requester watched every episode
   OR no play at all for 60 days        Tautulli "Last play date (including unfinished)"
                                       older than 60 days, or never played and
                                       added > 60 days ago
                                       (only for releases older than 1 year)
   OR no play at all for 90 days        same, any release age
                                       (so releases from the last year get 90 days)
)
```

Notes for building such rules in Maintainerr:

- Use **Tautulli → Last play date (including unfinished)**. Plex's "Last view
  date" only counts finished views, so a partly watched movie looks idle.
- Inside one section Maintainerr evaluates rules strictly left to right (no
  AND-before-OR precedence). Put each `A AND B` clause in its own section and
  join sections with `OR`.
- Use **Not exists** on the last-play date to catch never-played items.
- With the gate in place, the collection's "delete after" can be 1 day: the
  seeding threshold, not a grace period, is what keeps files around.
- Tag anything you want to protect with the exclusion tag (`dnd`) and enable
  tag exclusions in Maintainerr's Radarr/Sonarr settings.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml   # then edit
```

The user running the scripts needs read access to all torrent and library
directories (inode lookups). Deletions go through Transmission RPC.

## Configuration

See `config.yaml.example`. Key settings:

| Key | Meaning |
|---|---|
| `transmission.url/username/password` | Transmission RPC |
| `seeding_time_threshold_days` | Minimum accumulated seeding time for private torrents (e.g. 90). `age_threshold_days` is accepted as a deprecated alias. |
| `torrent_directories` | Only torrents downloading into these are managed |
| `check_directories` | Library folders; files hardlinked here are "in the library" |
| `maintainerr.url` | Maintainerr base URL (gate only) |
| `maintainerr.path_mappings` | Prefix map from the paths Plex reports to host paths, e.g. `"/mnt/storage/": "/media/"` |
| `maintainerr.log_file` | Gate log file name (in `logging.dir`) |
| `logging.*` | See [Logging and debugging](#logging-and-debugging) |

## Scheduling (cron)

Both scripts share a lock so they never run at the same time. Times assume
Maintainerr's collection job at 04:05 and rules job at 05:05:

```cron
50 3 * * * cd /home/USER/transmission-seeds-cleaner && flock -n /tmp/seeds-cleaner.lock .venv/bin/python maintainerr_gate.py >/dev/null
30 6 * * * cd /home/USER/transmission-seeds-cleaner && flock -w 3600 /tmp/seeds-cleaner.lock .venv/bin/python torrent_checker.py --yes >/dev/null
```

If you change Maintainerr's job times, keep the gate **before** the collection
job.

## Logging and debugging

```yaml
logging:
  dir: "logs"             # relative to the working directory
  file: "torrent_checker.log"
  level: "DEBUG"          # detail written to the files
  console_level: "INFO"   # detail printed to stdout
  retention_days: 30      # files rotate at midnight
```

Files: `logs/torrent_checker.log` and `logs/maintainerr_gate.log` (plus dated
rotations). Every line carries a run ID (`[YYYYMMDD-HHMMSS-PID]`), so one run
can be isolated with `grep`.

What gets logged:

- **Gate:** start/end with a summary (`checked`, `released`, `postponed`,
  `unverifiable`, `errors`), one `POSTPONE` / `RELEASE` line per due item with
  the reason, the new Maintainerr deletion date, and at DEBUG every file path
  (Plex path → host path, `[MISSING]` marker) and every linked torrent with
  privacy, status, seeding time and files present.
- **Cleaner:** index size, counts per decision, one `FLAG` line per torrent to
  delete (reason, `delete_data`, size, group members), at DEBUG one `keep` line
  per kept torrent with the members it is waiting on, warnings for torrents
  whose files are missing, and the deletion outcome with expected freed space.

Useful queries:

```bash
# Last gate run summary
grep "maintainerr_gate done" logs/maintainerr_gate.log | tail -1
# Why is <title> still in Plex / when will it go?
grep -i "<title>" logs/maintainerr_gate.log | tail
# Why was torrent #123 deleted (or kept)?
grep "#123 " logs/torrent_checker.log | tail
# Everything from one run
grep "20261116-035001-12345" logs/*.log
# Problems only
grep -E "WARNING|ERROR" logs/*.log
```

Common issues:

| Symptom | Likely cause |
|---|---|
| Gate: `none of its N file(s) found on disk` | `path_mappings` doesn't match how Plex sees the library |
| Gate: `torrent index unavailable` | Transmission RPC down or wrong credentials |
| Item postponed every 7 days forever | A private torrent in its group is paused; resume or remove it |
| Cleaner keeps a group forever | A member never completes (partial cross-seed, stalled download); see `waiting_on=` |
| Cleaner: `has none of its N files on disk` | Data was removed outside Transmission; the torrent is removed (`missing_files`) |

## Requirements

- Python 3.8+
- `transmission-rpc`, `pyyaml`, `requests` (see `requirements.txt`)
- A filesystem with inode/hardlink support
- For the gate: Maintainerr with the media server configured
