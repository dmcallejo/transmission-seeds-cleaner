#!/usr/bin/env python3
"""
Maintainerr metadata health check.

Maintainerr only logs metadata problems as warnings, and a title it cannot
match is silently never cleaned up. This check finds:

Only requested titles (Radarr/Sonarr tag "request") are reported, since the
cleanup rules never touch anything else.

1. Metadata mismatches from Maintainerr's log (default: last 2h, i.e. the latest rule run):
   - the media server's year disagrees with TMDB/TVDB, so provider IDs are rejected
   - external IDs could not be resolved, so no Radarr/Sonarr item was found
   - the resolved IDs point at a different title than the Radarr/Sonarr item
     stored in the same folder (Plex matched the wrong title)
2. Requested titles (Radarr/Sonarr tag "request", with files) that have no
   matching item in the Plex library their Maintainerr rule covers.

New or changed findings are sent to Telegram through the bot/chat already
configured in Maintainerr; unchanged findings are re-sent once a week.
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from torrent_checker import ConfigLoader, setup_logging

STATE_FILE = '.maintainerr_health_state.json'
REMIND_EVERY = timedelta(days=7)

# Runs inside the Maintainerr container, where the *arr hostnames resolve.
ARR_DUMP_JS = r"""
const M='http://localhost:6246/api';
const get=async(u,o)=>{const r=await fetch(u,o);if(!r.ok)throw new Error(u+' '+r.status);return r.json()};
(async()=>{const out=[];
 for(const arr of ['radarr','sonarr']){
  for(const s of await get(`${M}/settings/${arr}`)){
   const h={headers:{'X-Api-Key':s.apiKey}};
   const tags=await get(`${s.url}/api/v3/tag`,h);
   const req=tags.find(t=>t.label==='request');
   const items=await get(`${s.url}/api/v3/${arr==='radarr'?'movie':'series'}`,h);
   out.push({arr,id:s.id,name:s.serverName,items:items.map(x=>({
     title:x.title,year:x.year,tmdb:x.tmdbId,tvdb:x.tvdbId,path:x.path,
     requested:!!req&&x.tags.includes(req.id),
     hasFile:arr==='radarr'?!!x.hasFile:(x.statistics?.episodeFileCount||0)>0}))});
  }}
 console.log(JSON.stringify(out));})().catch(e=>{console.error(e.message);process.exit(1)});
"""

LOG_PATTERNS = [
    ('year_mismatch', re.compile(
        r'Rejected direct provider IDs for media server item "(?P<title>.+?)" \((?P<year>\d+)\).*?'
        r'Disagreements: (?P<detail>[^.]+)')),
    ('unresolved', re.compile(
        r"Failed to resolve external IDs for '(?P<title>.+?)' \(media server ID '(?P<msid>\d+)'\)\. "
        r"As a result, no (?P<arr>Sonarr|Radarr)")),
    ('not_in_arr', re.compile(
        r"None of the resolved external IDs \[(?P<ids>[^\]]*)\] for '(?P<title>.+?)' "
        r"\(media server ID '(?P<msid>\d+)'(?:, path '(?P<path>[^']*)')?\) matched a "
        r"(?:movie|series) in (?P<arr>Radarr|Sonarr)")),
]


def norm(text: str) -> str:
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


class Maintainerr:
    def __init__(self, url):
        self.url = url.rstrip('/')

    def get(self, path):
        with urllib.request.urlopen(f"{self.url}/api{path}", timeout=60) as r:
            return json.loads(r.read())

    def library(self, library_id):
        items, page = [], 1
        while True:
            data = self.get(f'/media-server/library/{library_id}/content?page={page}&limit=200')
            items.extend(data.get('items', []))
            if not data.get('items') or len(items) >= data.get('totalSize', 0):
                return items
            page += 1


def map_to_host(path: str, mappings: Dict[str, str]) -> str:
    for src, dst in mappings.items():
        if path.startswith(src):
            return dst + path[len(src):]
    return path


def scan_logs(container: str, since: str, arr_data, mappings) -> List[Dict]:
    out = subprocess.run(['docker', 'logs', '--since', since, container],
                         capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    # Only requested titles matter: the cleanup rules never touch anything else.
    requested_titles = {norm(i['title']) for inst in arr_data for i in inst['items'] if i['requested']}
    arr_by_folder = {}
    for inst in arr_data:
        for item in inst['items']:
            if item.get('path'):
                arr_by_folder[item['path'].rstrip('/')] = (inst, item)

    findings: Dict[str, Dict] = {}
    for line in text.splitlines():
        for kind, rx in LOG_PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            g = m.groupdict()
            title = g['title']
            if kind in ('year_mismatch', 'unresolved') and norm(title) not in requested_titles:
                break
            if kind == 'year_mismatch':
                key = f"meta:{norm(title)}"
                findings.setdefault(key, {'title': title, 'kind': 'metadata', 'msgs': set()})['msgs'].add(
                    f"Plex year {g['year']} rejected ({g['detail'].strip()})")
            elif kind == 'unresolved':
                key = f"meta:{norm(title)}"
                findings.setdefault(key, {'title': title, 'kind': 'metadata', 'msgs': set()})['msgs'].add(
                    f"no {g['arr']} item could be identified (Plex id {g['msid']})")
            elif kind == 'not_in_arr' and g.get('path'):
                # Only actionable when the *arr tracks something in that very
                # folder: then Plex matched the wrong title. Untracked titles
                # (added outside Radarr/Sonarr) are expected and ignored.
                folder = map_to_host(g['path'], mappings).rstrip('/')
                hit = None
                for candidate in (folder, str(Path(folder).parent)):
                    hit = arr_by_folder.get(candidate) or hit
                if hit and hit[1]['requested'] and norm(hit[1]['title']) != norm(title):
                    key = f"wrongmatch:{folder}"
                    findings[key] = {'title': hit[1]['title'], 'kind': 'metadata', 'msgs': {
                        f"Plex matched '{title}' ({g['ids']}) but {hit[0]['name']} has "
                        f"'{hit[1]['title']} ({hit[1]['year']})' in that folder"}}
            break
    return list(findings.values())


def other_library(maintainerr, lib_cache, covered, id_key, wanted, all_covered=frozenset()):
    """Name of another Plex library holding this ID, if any."""
    if 'all' not in lib_cache:
        lib_cache['all'] = [l for l in maintainerr.get('/media-server/libraries')]
    for lib in lib_cache['all']:
        lib_id = str(lib.get('id'))
        if lib_id in covered:
            continue
        if lib_id not in lib_cache:
            lib_cache[lib_id] = maintainerr.library(lib_id)
        for p in lib_cache[lib_id]:
            if str(wanted) in [str(i) for i in (p.get('providerIds') or {}).get(id_key, [])]:
                return lib.get('title', lib_id), lib_id in all_covered
    return None


def missing_requested(maintainerr: Maintainerr, arr_data) -> List[Dict]:
    findings = []
    collections = maintainerr.get('/collections')
    libs_by_arr = {}
    for c in collections:
        if c.get('radarrSettingsId'):
            libs_by_arr.setdefault(('radarr', c['radarrSettingsId']), set()).add(str(c['libraryId']))
        if c.get('sonarrSettingsId'):
            libs_by_arr.setdefault(('sonarr', c['sonarrSettingsId']), set()).add(str(c['libraryId']))
    lib_cache = {}
    for inst in arr_data:
        libs = libs_by_arr.get((inst['arr'], inst['id']))
        if not libs:
            continue
        plex = []
        for lib in libs:
            if lib not in lib_cache:
                lib_cache[lib] = maintainerr.library(lib)
            plex += lib_cache[lib]
        id_key = 'tmdb' if inst['arr'] == 'radarr' else 'tvdb'
        ids = {str(i) for p in plex for i in (p.get('providerIds') or {}).get(id_key, [])}
        titles = {(norm(p['title']), p.get('year')) for p in plex}
        by_title = {norm(p['title']): p for p in plex}
        for item in inst['items']:
            if not (item['requested'] and item['hasFile']):
                continue
            if str(item[id_key]) in ids:
                continue
            elsewhere = other_library(maintainerr, lib_cache, libs, id_key, item[id_key],
                                      {l for ls in libs_by_arr.values() for l in ls})
            if elsewhere and elsewhere[1]:
                findings.append({'title': f"{item['title']} ({item['year']})", 'kind': 'missing', 'msgs': {
                    f"requested in {inst['name']}, but Plex only has it in '{elsewhere[0]}' "
                    f"(no copy in the library {inst['name']}'s rule covers)"}})
                continue
            if elsewhere:
                findings.append({'title': f"{item['title']} ({item['year']})", 'kind': 'uncovered', 'msgs': {
                    f"in Plex library '{elsewhere[0]}', which no Maintainerr rule covers"}})
                continue
            same = by_title.get(norm(item['title']))
            if same:
                ptmdb = (same.get('providerIds') or {}).get(id_key) or ['none']
                msg = (f"in Plex as '{same['title']} ({same.get('year')})' but with {id_key.upper()} "
                       f"{ptmdb[0]} instead of {item[id_key]}: wrong or missing Plex match")
            else:
                msg = (f"requested in {inst['name']} with files, but no Plex item in the "
                       f"library its rule covers (not scanned, or titled differently)")
            findings.append({'title': f"{item['title']} ({item['year']})", 'kind': 'missing', 'msgs': {msg}})
    return findings


def telegram(maintainerr: Maintainerr, text: str) -> None:
    import notify
    notify.send(maintainerr.url, text)


def html(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('-c', '--config', default='config.yaml')
    parser.add_argument('-n', '--dry-run', action='store_true', help='Print the message, do not send')
    parser.add_argument('--force', action='store_true', help='Send even if nothing changed')
    parser.add_argument('--since', default='2h', help='Log window to scan (docker logs --since)')
    args = parser.parse_args()

    config = ConfigLoader.load(args.config)
    mcfg = config.get('maintainerr', {}) or {}
    hcfg = mcfg.get('health', {}) or {}
    log_cfg = dict(config.get('logging', {}) or {})
    log_cfg['file'] = hcfg.get('log_file', 'maintainerr_health.log')
    setup_logging({'logging': log_cfg}, default_file='maintainerr_health.log')
    logging.info("=== maintainerr_health start (logs since %s%s) ===", args.since, ', dry run' if args.dry_run else '')

    maintainerr = Maintainerr(mcfg.get('url', 'http://localhost:6246'))
    container = hcfg.get('container', 'maintainerr')
    ignore = {norm(t) for t in hcfg.get('ignore', [])}

    dump = subprocess.run(['docker', 'exec', '-i', container, 'node', '-'], input=ARR_DUMP_JS,
                          capture_output=True, text=True, timeout=180)
    if dump.returncode != 0:
        raise RuntimeError(f"reading Radarr/Sonarr through {container} failed: {dump.stderr.strip()}")
    arr_data = json.loads(dump.stdout)
    logging.info("Radarr/Sonarr instances: %s", ', '.join(
        f"{i['name']} ({sum(x['requested'] for x in i['items'])} requested)" for i in arr_data))

    findings = scan_logs(container, args.since, arr_data, mcfg.get('path_mappings', {}))
    meta_titles = {norm(f['title']) for f in findings}
    findings += [f for f in missing_requested(maintainerr, arr_data)
                 if norm(re.sub(r'\s*\(\d{4}\)$', '', f['title'])) not in meta_titles]
    findings = [f for f in findings if norm(re.sub(r'\s*\(\d{4}\)$', '', f['title'])) not in ignore]
    findings.sort(key=lambda f: (f['kind'], f['title'].lower()))
    for f in findings:
        logging.info("ISSUE [%s] %s: %s", f['kind'], f['title'], '; '.join(sorted(f['msgs'])))

    fingerprint = hashlib.sha256(json.dumps(
        [(f['kind'], f['title'], sorted(f['msgs'])) for f in findings]).encode()).hexdigest()
    state_path = Path(STATE_FILE)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    last_sent = datetime.fromisoformat(state['sent_at']) if state.get('sent_at') else None
    changed = state.get('fingerprint') != fingerprint
    due = bool(findings) and (args.force or changed or not last_sent or datetime.now() - last_sent >= REMIND_EVERY)

    if not findings:
        logging.info("No metadata issues found")
        if state.get('fingerprint') and state.get('count') and not args.dry_run:
            telegram(maintainerr, "✅ <b>Maintainerr</b>: all previously reported metadata issues are resolved.")
            logging.info("Sent all-clear message")
        state = {'fingerprint': fingerprint, 'count': 0, 'sent_at': None}
    elif due:
        meta = [f for f in findings if f['kind'] == 'metadata']
        missing = [f for f in findings if f['kind'] == 'missing']
        uncovered = [f for f in findings if f['kind'] == 'uncovered']
        lines = [f"⚠️ <b>Maintainerr</b>: {len(findings)} title(s) can't be evaluated by the cleanup rules"
                 + ("" if changed else " (reminder, unchanged)")]
        if meta:
            lines.append("\n<b>Metadata mismatch</b> (fix the match / Originally Available date in Plex):")
            lines += [f"• {html(f['title'])}: {html('; '.join(sorted(f['msgs'])))}" for f in meta]
        if missing:
            lines.append("\n<b>Requested but not matched in Plex</b>:")
            lines += [f"• {html(f['title'])}: {html('; '.join(sorted(f['msgs'])))}" for f in missing]
        if uncovered:
            lines.append("\n<b>Requested, but in a library with no Maintainerr rule</b> (never cleaned up):")
            lines += [f"• {html(f['title'])}: {html('; '.join(sorted(f['msgs'])))}" for f in uncovered]
        lines.append("\nAfter fixing in Plex, restart Maintainerr to drop its metadata cache.")
        text = '\n'.join(lines)[:4000]
        if args.dry_run:
            print(text)
            logging.info("Dry run: message not sent")
        else:
            telegram(maintainerr, text)
            logging.info("Sent Telegram message (%d issue(s), %s)", len(findings), 'changed' if changed else 'reminder')
            state = {'fingerprint': fingerprint, 'count': len(findings), 'sent_at': datetime.now().isoformat()}
    else:
        logging.info("%d issue(s) unchanged since %s; not re-sending yet", len(findings), last_sent)

    if not args.dry_run:
        state_path.write_text(json.dumps(state))
    logging.info("=== maintainerr_health done: %d issue(s) ===", len(findings))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.error("Health check failed: %s", e)
        logging.debug("Traceback:", exc_info=True)
        sys.exit(1)
