#!/usr/bin/env python3
"""
Maintainerr deletion gate.

Run shortly before Maintainerr's collection handler. For every collection item
that is due for deletion, find every torrent that shares its files (the
original download plus any cross-seed copies). If any of them still has to
seed (private torrent below the seeding threshold), postpone the item in
Maintainerr until the last one is done, so the title stays on Plex while its
files have to stay on disk anyway.
"""

import argparse
import json
import logging
import math
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from torrent_checker import ConfigLoader, TorrentLinkIndex, TransmissionClient, setup_logging

DAY = 86400
# A private torrent that is paused accumulates no seeding time; re-check weekly.
STALLED_RECHECK_DAYS = 7


class Maintainerr:
    def __init__(self, url: str):
        self.url = url.rstrip('/')

    def _request(self, path: str, body: Optional[Dict] = None, method: str = 'GET'):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}/api{path}", data=data, method=method,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b'null')

    def collections(self) -> List[Dict]:
        return [c for c in self._request('/collections') if c.get('isActive')]

    def collection_items(self, collection_id: int) -> List[Dict]:
        items, page = [], 1
        while True:
            data = self._request(f'/collections/media/{collection_id}/content/{page}')
            batch = data.get('items', [])
            items.extend(batch)
            if not batch or len(items) >= data.get('totalSize', 0):
                return items
            page += 1

    def media_files(self, media_server_id: str) -> List[str]:
        meta = self._request(f'/media-server/meta/{media_server_id}')
        if meta.get('type') in ('season', 'show'):
            episodes = self._request(f'/media-server/meta/{media_server_id}/children')
            if meta.get('type') == 'show':
                episodes = [
                    ep for season in episodes
                    for ep in self._request(f"/media-server/meta/{season['id']}/children")
                ]
            return [ep['path'] for ep in episodes if ep.get('path')]
        return [meta['path']] if meta.get('path') else []

    def postpone(self, collection_id: int, media_id: str, days: int) -> Dict:
        return self._request(
            '/collections/media/postpone',
            {'collectionId': collection_id, 'mediaId': str(media_id), 'days': days},
            method='POST',
        )


def map_path(path: str, mappings: Dict[str, str]) -> Path:
    for src, dst in mappings.items():
        if path.startswith(src):
            return Path(dst + path[len(src):])
    return Path(path)


def check_item(item, collection, maintainerr, index, mappings, dry_run, summary):
    media_data = item.get('mediaData') or {}
    title = media_data.get('title', item['mediaServerId'])
    parent = media_data.get('parentTitle')
    label = f"[{collection['title']}] {parent + ' - ' if parent else ''}{title} (id {item['mediaServerId']})"
    due = item['_due']

    def postpone(days, reason):
        summary['postponed'] += 1
        logging.info("POSTPONE %s by %d day(s): %s", label, days, reason)
        if dry_run:
            return
        result = maintainerr.postpone(collection['id'], item['mediaServerId'], days)
        logging.info("  Maintainerr deletion date is now %s", result.get('deletionDate'))

    if index is None:
        summary['unverifiable'] += 1
        postpone(1, "torrent index unavailable (Transmission unreachable?)")
        return

    plex_paths = maintainerr.media_files(item['mediaServerId'])
    files = [map_path(p, mappings) for p in plex_paths]
    missing = [f for f in files if not f.exists()]
    logging.debug("%s: due %s, %d file(s) from Plex", label, due.date(), len(files))
    for plex_path, host_path in zip(plex_paths, files):
        logging.debug("  file %s -> %s%s", plex_path, host_path, '' if host_path.exists() else '  [MISSING]')

    # Without the files we cannot tell which torrents depend on them.
    # Never let that silently become "no torrents, free to delete".
    if not files or len(missing) == len(files):
        summary['unverifiable'] += 1
        postpone(1, f"none of its {len(files)} file(s) found on disk; check path_mappings")
        return
    if missing:
        logging.warning("%s: %d of %d file(s) missing on disk (checking the rest)",
                        label, len(missing), len(files))

    torrent_ids = set()
    for torrent_id in index.torrents_for_paths(files):
        torrent_ids |= set(index.group_members(torrent_id))
    for torrent_id in sorted(torrent_ids):
        logging.debug("  linked %s", index.describe(torrent_id))

    if not torrent_ids:
        summary['no_torrents'] += 1
        logging.info("RELEASE %s: no torrents share its files (due %s)", label, due.date())
        return

    waits = []
    for torrent_id in sorted(torrent_ids):
        remaining = index.remaining_seconds(torrent_id)
        if remaining == 0:
            continue
        info = index.torrents[torrent_id]
        if remaining is None or info['status'] in ('stopped', 'check pending', 'checking'):
            days = STALLED_RECHECK_DAYS
            why = f"#{torrent_id} {info['name']} ({info['status']}, seeding time not advancing)"
        else:
            days = max(1, math.ceil(remaining / DAY))
            why = f"#{torrent_id} {info['name']} ({remaining / DAY:.1f} days left)"
        waits.append((days, why))

    if not waits:
        summary['released'] += 1
        logging.info("RELEASE %s: all %d linked torrent(s) done seeding (due %s)",
                     label, len(torrent_ids), due.date())
        return
    postpone(max(d for d, _ in waits), 'waiting on ' + '; '.join(w for _, w in waits))


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('-c', '--config', default='config.yaml')
    parser.add_argument('-n', '--dry-run', action='store_true', help='Report only, do not postpone')
    parser.add_argument('--lookahead-hours', type=float, default=24,
                        help='Treat items due within this many hours as due (default 24)')
    parser.add_argument('--all', action='store_true', help='Check every item, not only due ones')
    args = parser.parse_args()

    config = ConfigLoader.load(args.config)
    gate_cfg = config.get('maintainerr', {}) or {}
    log_cfg = dict(config.get('logging', {}) or {})
    log_cfg['file'] = gate_cfg.get('log_file', 'maintainerr_gate.log')
    if 'log_level' in gate_cfg:
        log_cfg['level'] = gate_cfg['log_level']
    setup_logging({'logging': log_cfg}, default_file='maintainerr_gate.log')

    threshold_days = config.get('seeding_time_threshold_days', config.get('age_threshold_days'))
    logging.info("=== maintainerr_gate start (threshold %s days, lookahead %sh%s%s) ===",
                 threshold_days, args.lookahead_hours,
                 ', dry run' if args.dry_run else '', ', all items' if args.all else '')

    trans = config['transmission']
    try:
        transmission = TransmissionClient(trans['url'], trans['username'], trans['password'])
        index = TorrentLinkIndex(
            transmission, transmission.get_all_torrents(),
            config.get('check_directories', []), config.get('torrent_directories', []),
            threshold_days,
        )
        logging.info("Indexed %d torrents in %d groups", len(index.torrents), len(index.groups()))
    except Exception as e:
        # Fail safe: without the torrent index nothing can be verified, so
        # every due item is held back one day instead of being deleted.
        logging.error("Cannot build torrent index (%s); postponing every due item by 1 day", e)
        logging.debug("Traceback:", exc_info=True)
        index = None
    maintainerr = Maintainerr(gate_cfg.get('url', 'http://localhost:6246'))
    mappings = gate_cfg.get('path_mappings', {})

    horizon = datetime.now(timezone.utc) + timedelta(hours=args.lookahead_hours)
    summary = {'items': 0, 'checked': 0, 'released': 0, 'no_torrents': 0,
               'postponed': 0, 'unverifiable': 0, 'errors': 0}

    for collection in maintainerr.collections():
        delete_after = collection.get('deleteAfterDays') or 0
        items = maintainerr.collection_items(collection['id'])
        logging.info("Collection '%s' (id %s, delete after %s day(s)): %d item(s)",
                     collection['title'], collection['id'], delete_after, len(items))
        for item in items:
            summary['items'] += 1
            add_date = datetime.fromisoformat(item['addDate'].replace('Z', '+00:00'))
            item['_due'] = add_date + timedelta(days=delete_after)
            if not args.all and item['_due'] > horizon:
                logging.debug("  skip (not due until %s): %s", item['_due'].date(),
                              (item.get('mediaData') or {}).get('title', item['mediaServerId']))
                continue
            summary['checked'] += 1
            try:
                check_item(item, collection, maintainerr, index, mappings, args.dry_run, summary)
            except Exception as e:
                summary['errors'] += 1
                logging.error("Item %s in '%s' failed: %s", item.get('mediaServerId'), collection['title'], e)
                logging.debug("Traceback:", exc_info=True)
                try:  # hold it back rather than let it be deleted unchecked
                    if not args.dry_run:
                        maintainerr.postpone(collection['id'], item['mediaServerId'], 1)
                    logging.info("  postponed 1 day because the check failed")
                except Exception as e2:
                    logging.error("  could not postpone either: %s", e2)

    logging.info("=== maintainerr_gate done%s: %s ===", ' (dry run)' if args.dry_run else '', summary)
    if summary['errors']:
        sys.exit(2)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        logging.error("Gate failed: %s", e)
        logging.debug("Traceback:", exc_info=True)
        sys.exit(1)
