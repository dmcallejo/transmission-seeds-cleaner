"""
Duplicate-copy check: find torrent files that are separate copies of another
file (in the library or another torrent) and could be hardlinks instead.

Candidates are files >= min_size_mb on the same filesystem with identical size
and identical sampled hash (start/middle/end). A pair only counts as a
duplicate after a full SHA-256 of both files matches. Full hashes are cached
by (device, inode, size, mtime) so unchanged files are not re-read nightly.

With relinking enabled, each duplicate path is replaced by a hardlink to the
copy that is kept (library copy first, then the inode with most links, then a
non-cross-seed download). Torrents using a replaced path are paused during the
swap, resumed if they were running, and queued for a Transmission re-check.
"""

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

SAMPLE = 4 * 1024 ** 2
CACHE_FILE = '.dedupe_hash_cache.json'


def _sample_hash(path: str, size: int) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for off in (0, size // 2, max(0, size - SAMPLE)):
            f.seek(off)
            h.update(f.read(SAMPLE))
    return h.hexdigest()


def _full_hash(path: str, cache: Dict, st: os.stat_result) -> str:
    key = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{int(st.st_mtime)}"
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 ** 2), b''):
            h.update(chunk)
    cache[key] = h.hexdigest()
    return cache[key]


def find_duplicates(index, library_dirs: List[str], torrent_dirs: List[str],
                    min_size: int, cache: Dict) -> List[Dict]:
    """Return groups of distinct inodes with identical content, at least one owned by a torrent."""
    path_owner: Dict[str, set] = defaultdict(set)
    for tid, info in index.torrents.items():
        for p in info['file_keys']:
            path_owner[p].add(tid)

    by_size = defaultdict(lambda: defaultdict(list))  # size -> (dev, ino) -> [paths]
    for root in library_dirs + torrent_dirs:
        for p in Path(root).rglob('*'):
            try:
                if p.is_file():
                    st = p.stat()
                    if st.st_size >= min_size:
                        by_size[st.st_size][(st.st_dev, st.st_ino)].append(str(p))
            except OSError:
                continue

    groups = []
    for size, inodes in by_size.items():
        if len(inodes) < 2:
            continue
        if not any(p in path_owner for paths in inodes.values() for p in paths):
            continue
        by_dev = defaultdict(dict)
        for (dev, ino), paths in inodes.items():
            by_dev[dev][(dev, ino)] = paths
        for dev, same_dev in by_dev.items():
            if len(same_dev) < 2:
                continue
            sampled = defaultdict(list)
            for key, paths in same_dev.items():
                try:
                    sampled[_sample_hash(paths[0], size)].append((key, paths))
                except OSError as e:
                    logging.debug("dedupe: cannot read %s: %s", paths[0], e)
            for candidates in sampled.values():
                if len(candidates) < 2:
                    continue
                full = defaultdict(list)
                for key, paths in candidates:
                    try:
                        full[_full_hash(paths[0], cache, os.stat(paths[0]))].append((key, paths))
                    except OSError as e:
                        logging.debug("dedupe: cannot hash %s: %s", paths[0], e)
                for digest, same in full.items():
                    if len(same) < 2:
                        continue
                    copies = []
                    for (d, ino), paths in same:
                        copies.append({
                            'inode': ino, 'paths': paths,
                            'library': any(p.startswith(tuple(library_dirs)) for p in paths),
                            'torrents': sorted({t for p in paths for t in path_owner.get(p, ())}),
                            'cross_seed': all('/cross-seed/' in p for p in paths),
                            'nlink': os.stat(paths[0]).st_nlink,
                        })
                    # keep: library copy, then most links, then not cross-seed
                    copies.sort(key=lambda c: (not c['library'], -c['nlink'], c['cross_seed']))
                    groups.append({'size': size, 'sha256': digest, 'keep': copies[0], 'replace': copies[1:]})
    return groups


def relink(transmission, groups: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Replace duplicate paths with hardlinks to the kept copy. Returns (relinked, failed) event lists."""
    client = transmission.client
    targets = {p for g in groups for c in g['replace'] for p in c['paths']}
    torrent_ids = sorted({t for g in groups for c in g['replace'] for t in c['torrents']})
    if not targets:
        return [], []
    was_running = []
    if torrent_ids:
        for t in client.get_torrents(torrent_ids, arguments=['id', 'status']):
            if str(t.status) != 'stopped':
                was_running.append(t.id)
        client.stop_torrent(torrent_ids)
        time.sleep(3)
    done, failed = [], []
    try:
        for g in groups:
            keep = g['keep']['paths'][0]
            group_ok = True
            for copy in g['replace']:
                for path in copy['paths']:
                    try:
                        ks, rs = os.stat(keep), os.stat(path)
                        if ks.st_ino == rs.st_ino:
                            continue
                        if ks.st_size != rs.st_size or ks.st_dev != rs.st_dev:
                            raise RuntimeError('size or device changed since hashing')
                        tmp = path + '.dedupe-tmp'
                        os.link(keep, tmp)
                        os.replace(tmp, path)
                        logging.info("dedupe: relinked %s -> inode %s", path, ks.st_ino)
                    except Exception as e:
                        group_ok = False
                        failed.append({'path': path, 'torrents': copy['torrents'], 'error': str(e)})
                        logging.error("dedupe: could not relink %s: %s", path, e)
            if group_ok:
                done.append({'size': g['size'] * len(g['replace']), 'keep': keep,
                             'paths': [p for c in g['replace'] for p in c['paths']],
                             'torrents': sorted({t for c in g['replace'] for t in c['torrents']}
                                                | set(g['keep']['torrents']))})
    finally:
        if was_running:
            client.start_torrent(was_running)
        if torrent_ids:
            client.verify_torrent(torrent_ids)
            logging.info("dedupe: resumed %s, queued re-check for %s", was_running, torrent_ids)
    return done, failed


def run(transmission, config: Dict, index_factory, relink_override: bool = None) -> Dict:
    """Returns {'found': n, 'recoverable': bytes, 'relinked': [...], 'failed': [...], 'names': {id: name}}."""
    cfg = config.get('dedupe', {}) or {}
    summary = {'found': 0, 'recoverable': 0, 'relinked': [], 'failed': [], 'names': {}}
    if not cfg.get('enabled', False):
        return summary
    do_relink = cfg.get('relink', False) if relink_override is None else relink_override
    min_size = int(cfg.get('min_size_mb', 50)) * 1024 ** 2
    cache_path = Path(cfg.get('cache_file', CACHE_FILE))
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except ValueError:
        cache = {}

    logging.info("dedupe: scanning for duplicate copies (>= %d MB, relink=%s)", min_size // 1024 ** 2, do_relink)
    index = index_factory()
    groups = find_duplicates(index, config.get('check_directories', []),
                             config.get('torrent_directories', []), min_size, cache)
    cache_path.write_text(json.dumps(cache))

    wasted = sum(g['size'] * len(g['replace']) for g in groups)
    summary.update(found=len(groups), recoverable=wasted,
                   names={tid: info['name'] for tid, info in index.torrents.items()})
    logging.info("dedupe: %d duplicate group(s), %.1f GiB recoverable", len(groups), wasted / 1024 ** 3)
    for g in groups:
        def fmt(c):
            tags = (['library'] if c['library'] else []) + [f"#{t}" for t in c['torrents']]
            return f"inode {c['inode']} [{', '.join(tags) or 'no torrent'}] {c['paths'][0]}"
        logging.info("DUPLICATE %.2f GiB sha256=%s keep %s", g['size'] / 1024 ** 3, g['sha256'][:12], fmt(g['keep']))
        for c in g['replace']:
            logging.info("  replace %s", fmt(c))
    if groups and do_relink:
        done, failed = relink(transmission, groups)
        summary.update(relinked=done, failed=failed)
        logging.info("dedupe: %d group(s) relinked, %d path(s) failed", len(done), len(failed))
    elif groups:
        logging.info("dedupe: report only (set dedupe.relink: true or pass --relink to fix)")
    return summary
