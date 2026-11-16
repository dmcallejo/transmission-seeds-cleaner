#!/usr/bin/env python3
"""
Transmission Torrent Hardlink Checker

Identifies seeding torrents that have seeded longer than a specified threshold, checks if their
files are hardlinked into a target directory or other torrents, and finds
torrents that are no longer registered at their tracker.
"""

import os
import sys
import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
import argparse

import yaml
import transmission_rpc


class ConfigLoader:
    """Loads and validates configuration from YAML file."""
    
    @staticmethod
    def load(config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            ConfigLoader._validate_config(config)
            return config
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in configuration file: {e}")
    
    @staticmethod
    def _validate_config(config: Dict) -> None:
        """Validate required configuration fields."""
        if 'transmission' not in config:
            raise ValueError("Missing required configuration key: 'transmission'")
        if 'seeding_time_threshold_days' not in config and 'age_threshold_days' not in config:
            raise ValueError(
                "Missing required configuration key: 'seeding_time_threshold_days'"
            )
        threshold_days = config.get(
            'seeding_time_threshold_days', config.get('age_threshold_days')
        )
        if (
            not isinstance(threshold_days, (int, float))
            or isinstance(threshold_days, bool)
            or (isinstance(threshold_days, float) and not math.isfinite(threshold_days))
            or threshold_days < 0
        ):
            raise ValueError("Seeding time threshold must be a non-negative number of days")
        # Validate that at least one directory option is specified for check directories
        has_check_dir = 'check_directory' in config and config['check_directory'] is not None
        has_check_dirs = 'check_directories' in config and config['check_directories'] is not None
        if not (has_check_dir or has_check_dirs):
            raise ValueError("Must specify either 'check_directory' or 'check_directories' in config")
        
        # Validate that at least one directory option is specified for torrent directories
        has_torrent_dir = 'torrent_directory' in config and config['torrent_directory'] is not None
        has_torrent_dirs = 'torrent_directories' in config and config['torrent_directories'] is not None
        if not (has_torrent_dir or has_torrent_dirs):
            raise ValueError("Must specify either 'torrent_directory' or 'torrent_directories' in config")
        
        trans_config = config['transmission']
        trans_required = ['url', 'username', 'password']
        for key in trans_required:
            if key not in trans_config:
                raise ValueError(f"Missing required transmission config: {key}")


class TransmissionClient:
    """Wrapper for Transmission RPC client."""

    # These indicate that the info hash is not present at the tracker. Other
    # errors (timeouts, DNS failures, rate limiting, etc.) can be temporary.
    _TORRENT_UNAVAILABLE_PATTERNS = (
        re.compile(r"\btorrent\b.{0,50}\b(not registered|unregistered|not found|does not exist|unknown)\b"),
        re.compile(r"\b(info[ _-]?hash|infohash)\b.{0,50}\b(not registered|not found|does not exist|unknown)\b"),
        re.compile(r"\b(unregistered|not registered)\s+torrent\b"),
        re.compile(r"\btorrent\b.{0,80}\b(?:has been|was)\s+deleted\b"),
        re.compile(r"\btorrent\b.{0,80}\bis\s+not\s+authorized\s+for\s+use\s+(?:on|with)\s+(?:this\s+)?tracker\b"),
    )
    _CORRUPTION_PATTERN = re.compile(r"\bcorrupt(?:ed|ion)?\b", re.IGNORECASE)
    
    def __init__(self, url: str, username: str, password: str):
        """Initialize Transmission client."""
        # Parse URL to extract host, port, and protocol
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or 'localhost'
        
        # Determine protocol (http or https)
        protocol = parsed.scheme or 'http'
        
        # Use standard ports if not specified
        if parsed.port:
            port = parsed.port
        elif protocol == 'https':
            port = 443
        else:
            port = 80
        
        self.client = transmission_rpc.Client(
            host=host,
            port=port,
            username=username,
            password=password,
            protocol=protocol
        )

    def get_all_torrents(self) -> List:
        """Fetch one complete Transmission torrent snapshot."""
        return self.client.get_torrents()

    def verify_corrupt_torrents(self, torrents: List) -> List[int]:
        """Trigger batched verification for torrents with a current corruption error."""
        torrent_ids = []
        for torrent in torrents:
            error_string = self._get_attr(torrent, 'error_string', '') or ''
            corruption_message = str(error_string).strip()
            # corruptEver is cumulative historical data, not an indication
            # that verification is currently required. Only a current
            # corruption error should trigger a new verification.
            reports_corruption = self._CORRUPTION_PATTERN.search(corruption_message) is not None
            if not reports_corruption:
                continue

            status = str(self._get_attr(torrent, 'status', '')).lower()
            if status in {'checking', 'check pending'}:
                logging.debug(
                    "Skipping verification for torrent %s; verification is already active",
                    self._get_attr(torrent, 'id', '<unknown>'),
                )
                continue
            torrent_id = self._get_attr(torrent, 'id')
            if torrent_id is not None:
                torrent_ids.append(torrent_id)

        if not torrent_ids:
            return []

        try:
            # transmission-rpc accepts a list of IDs, so use one request for
            # the whole batch instead of one request per torrent.
            self.client.verify_torrent(torrent_ids)
            logging.info("Triggered data verification for %d corrupt torrent(s)", len(torrent_ids))
            return torrent_ids
        except Exception as e:
            logging.error("Failed to trigger data verification: %s", e)
            return []
    
    def get_seeding_torrents(
        self, seeding_time_threshold_days: float, torrents: Optional[List] = None
    ) -> List[Dict]:
        """
        Get seeding torrents whose accumulated seeding time meets the threshold.
        
        Returns:
            List of torrent dictionaries with identity, timestamps, seeding time, files, and peer count.
        """
        threshold_seconds = seeding_time_threshold_days * 24 * 60 * 60
        if torrents is None:
            torrents = self.client.get_torrents()
        
        eligible_seeding = []
        for torrent in torrents:
            # Check if torrent is seeding
            if self._get_attr(torrent, 'status') != 'seeding':
                continue

            torrent_name = self._get_attr(torrent, 'name', '<unnamed torrent>')
            is_private = self._get_attr(torrent, 'is_private')
            seeding_seconds = self._get_attr(torrent, 'seconds_seeding')
            if is_private is not False:
                if (
                    not isinstance(seeding_seconds, (int, float))
                    or isinstance(seeding_seconds, bool)
                    or (isinstance(seeding_seconds, float) and not math.isfinite(seeding_seconds))
                    or seeding_seconds < 0
                ):
                    logging.warning(
                        "Could not determine seeding time for private or privacy-unknown torrent %s; skipping it",
                        torrent_name,
                    )
                    continue
                if seeding_seconds < threshold_seconds:
                    continue
            elif (
                not isinstance(seeding_seconds, (int, float))
                or isinstance(seeding_seconds, bool)
                or (isinstance(seeding_seconds, float) and not math.isfinite(seeding_seconds))
                or seeding_seconds < 0
            ):
                # Public torrents bypass the duration filter; retain a valid
                # duration for reporting when Transmission provides one.
                seeding_seconds = None

            # Added date is report metadata only; eligibility comes from the
            # accumulated seeding time reported by Transmission.
            added_date = None
            for attr_name in ('added_date', 'add_date', 'date_added'):
                added_date = self._get_attr(torrent, attr_name)
                if added_date is not None:
                    break
            if added_date is not None and added_date.tzinfo is None:
                added_date = added_date.replace(tzinfo=timezone.utc)

            if is_private is False or seeding_seconds >= threshold_seconds:
                # Get seed count from tracker stats (most reliable source)
                seed_count = 0
                
                # Try tracker_stats first - get seeder count from tracker scrape
                if hasattr(torrent, 'tracker_stats') and torrent.tracker_stats:
                    try:
                        for tracker_stat in torrent.tracker_stats:
                            if hasattr(tracker_stat, 'seeder_count'):
                                val = tracker_stat.seeder_count
                                if isinstance(val, int) and val > 0:
                                    seed_count = max(seed_count, val)  # Use max if multiple trackers
                    except Exception:
                        pass
                
                # Fallback to peers_sending_to_us if tracker stats unavailable
                if seed_count == 0:
                    if hasattr(torrent, 'peers_sending_to_us'):
                        val = torrent.peers_sending_to_us
                        if isinstance(val, int) and val >= 0:
                            seed_count = val
                
                eligible_seeding.append({
                    'id': self._get_attr(torrent, 'id'),
                    'name': torrent_name,
                    'added_date': added_date,
                    'seeding_time_seconds': seeding_seconds,
                    'is_private': is_private,
                    'files': self._get_torrent_files(torrent),
                    'download_dir': self._get_attr(torrent, 'download_dir', ''),
                    'peer_count': seed_count,
                    'torrent_obj': torrent  # Keep reference for seed count lookup
                })
        
        return eligible_seeding

    @staticmethod
    def _get_attr(obj, name: str, default=None):
        """Read an attribute from RPC objects and dicts used by test doubles."""
        camel_name = re.sub(r'_([a-z])', lambda match: match.group(1).upper(), name)
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            return obj.get(camel_name, default)
        for attr_name in dict.fromkeys((name, camel_name)):
            try:
                value = getattr(obj, attr_name, default)
            except (AttributeError, KeyError, TypeError):
                continue
            if value is not default:
                return value
        return default

    @classmethod
    def _is_torrent_unavailable_result(cls, result: str) -> bool:
        """Return whether a tracker response says the torrent is not registered."""
        if not isinstance(result, str):
            return False
        result = result.lower()
        return any(pattern.search(result) for pattern in cls._TORRENT_UNAVAILABLE_PATTERNS)

    @classmethod
    def _get_tracker_unavailable_reason(cls, torrent) -> Optional[str]:
        """
        Return a reason when every reported tracker definitively rejects the
        torrent, otherwise return None.

        Missing/empty tracker status is treated as unknown. This prevents a
        newly added torrent or a tracker that has not announced yet from being
        deleted accidentally.
        """
        try:
            tracker_stats = cls._get_attr(torrent, 'tracker_stats', []) or []
        except Exception:
            return None
        if not tracker_stats:
            # Some Transmission/RPC combinations do not return trackerStats.
            # The aggregate error string is safe to use only when it contains
            # an explicit torrent-not-registered message.
            error_string = cls._get_attr(torrent, 'error_string', '') or ''
            if cls._is_torrent_unavailable_result(error_string):
                return error_string.strip()
            return None

        # A successful response from any tracker means the torrent is still
        # available. Temporary failures must never qualify for deletion.
        reasons = []
        reported_trackers = 0
        for tracker_stat in tracker_stats:
            announce_result = cls._get_attr(tracker_stat, 'last_announce_result', '') or ''
            scrape_result = cls._get_attr(tracker_stat, 'last_scrape_result', '') or ''
            announce_succeeded = cls._get_attr(tracker_stat, 'last_announce_succeeded', None)
            scrape_succeeded = cls._get_attr(tracker_stat, 'last_scrape_succeeded', None)

            # Announce responses are authoritative. A scrape may be
            # unsupported or fail independently of an announce, so only use
            # it when the tracker has not reported an announce result.
            if isinstance(announce_result, str) and announce_result.strip():
                reported_trackers += 1
                if announce_succeeded is True:
                    return None
                if not cls._is_torrent_unavailable_result(announce_result):
                    return None
                reasons.append(announce_result.strip())
                continue

            if scrape_succeeded is True:
                return None
            if not isinstance(scrape_result, str) or not scrape_result.strip():
                # Backup trackers often have no response because Transmission
                # never needed to contact them. They are not evidence that
                # the torrent is available, so ignore them.
                continue
            reported_trackers += 1
            if not cls._is_torrent_unavailable_result(scrape_result):
                return None
            reasons.append(scrape_result.strip())

        if not reported_trackers:
            error_string = cls._get_attr(torrent, 'error_string', '') or ''
            if cls._is_torrent_unavailable_result(error_string):
                return error_string.strip()
            return None
        if not reasons or reported_trackers != len(reasons):
            return None
        return '; '.join(dict.fromkeys(reasons))

    def get_unavailable_tracker_torrents(self, torrents: Optional[List] = None) -> List[Dict]:
        """Get torrents whose tracker responses definitively say they are gone."""
        unavailable = []
        if torrents is None:
            torrents = self.client.get_torrents()
        torrents_with_tracker_stats = 0
        for torrent in torrents:
            if self._get_attr(torrent, 'tracker_stats', []) or []:
                torrents_with_tracker_stats += 1
            reason = self._get_tracker_unavailable_reason(torrent)
            if reason is None:
                continue

            added_date = None
            for attr_name in ['addedDate', 'added_date', 'add_date', 'date_added']:
                value = self._get_attr(torrent, attr_name)
                if value is not None:
                    added_date = value
                    break
            if added_date is not None and added_date.tzinfo is None:
                added_date = added_date.replace(tzinfo=timezone.utc)

            files = self._get_torrent_files(torrent)
            unavailable.append({
                'id': self._get_attr(torrent, 'id'),
                'name': self._get_attr(torrent, 'name', '<unnamed torrent>'),
                'added_date': added_date,
                'seeding_time_seconds': self._get_attr(torrent, 'seconds_seeding'),
                'is_private': self._get_attr(torrent, 'is_private'),
                'files': files,
                'download_dir': self._get_attr(torrent, 'download_dir', ''),
                'peer_count': 0,
                'tracker_unavailable_reason': reason,
            })

        logging.debug(
            "Tracker status scan inspected %d torrent(s), %d with tracker stats, found %d unavailable",
            len(torrents),
            torrents_with_tracker_stats,
            len(unavailable),
        )
        return unavailable
    
    def _get_torrent_files(self, torrent) -> List[Dict]:
        """Extract file information from a torrent."""
        files = []
        try:
            # Current transmission-rpc exposes files through get_files().
            if hasattr(torrent, 'get_files') and callable(torrent.get_files):
                for file_obj in torrent.get_files():
                    files.append({
                        'name': file_obj.name,
                        'size': file_obj.size
                    })
                return files

            # Try different possible ways to access files
            if hasattr(torrent, 'files') and callable(torrent.files):
                file_dict = torrent.files()
                for file_obj in file_dict.values():
                    files.append({
                        'name': file_obj['name'],
                        'size': file_obj.get('size', file_obj.get('length', 0))
                    })
            elif hasattr(torrent, 'files'):
                # files might be a direct attribute
                file_list = torrent.files
                if isinstance(file_list, dict):
                    for file_obj in file_list.values():
                        files.append({
                            'name': file_obj['name'],
                            'size': file_obj.get('size', file_obj.get('length', 0))
                        })
                elif isinstance(file_list, list):
                    for file_obj in file_list:
                        files.append({
                            'name': file_obj.get('name', file_obj),
                            'size': file_obj.get('size', file_obj.get('length', 0))
                        })
        except Exception as e:
            logging.warning(f"Could not extract files from torrent: {e}")
        
        return files


class TorrentLinkIndex:
    """
    Index of every torrent's exact on-disk files and the inodes they share.

    Torrents are grouped when they share any inode: either through hardlinks
    (cross-seed links, *arr imports) or because two torrents point at the very
    same path. File lists always come from Transmission, never from scanning a
    torrent's folder, because several torrents can share one folder name
    (for example cross-seed season packs all called "Season 1").
    """

    def __init__(
        self,
        transmission: 'TransmissionClient',
        rpc_torrents: List,
        target_directories: List[str],
        torrent_directories: List[str],
        seeding_time_threshold_days: float,
    ):
        self.transmission = transmission
        self.threshold_seconds = seeding_time_threshold_days * 24 * 60 * 60
        self.target_directories = [self._existing_dir(d, 'Target') for d in target_directories]
        self.torrent_directories = [self._existing_dir(d, 'Torrent') for d in torrent_directories]

        self.torrents: Dict[int, Dict] = {}
        self.inode_to_torrents: Dict[Tuple[int, int], Set[int]] = {}
        self.path_to_torrents: Dict[str, Set[int]] = {}
        for raw in rpc_torrents:
            info = self._describe(raw)
            if info is None:
                continue
            self.torrents[info['id']] = info
            for path, key in info['file_keys'].items():
                self.inode_to_torrents.setdefault(key, set()).add(info['id'])
                self.path_to_torrents.setdefault(path, set()).add(info['id'])

        self.library_inodes = self._scan_library()
        self.group_of = self._build_groups()

    @staticmethod
    def _existing_dir(directory: str, label: str) -> Path:
        path = Path(directory)
        if not path.exists():
            raise ValueError(f"{label} directory does not exist: {directory}")
        return path

    def in_torrent_directories(self, path: Path) -> bool:
        for torrent_dir in self.torrent_directories:
            try:
                path.relative_to(torrent_dir)
                return True
            except ValueError:
                continue
        return False

    def _describe(self, raw) -> Optional[Dict]:
        get = self.transmission._get_attr
        torrent_id = get(raw, 'id')
        download_dir = get(raw, 'download_dir', '') or ''
        if torrent_id is None or not download_dir:
            return None
        files = self.transmission._get_torrent_files(raw)
        file_keys: Dict[str, Tuple[int, int]] = {}
        total_size = 0
        for file_info in files:
            name = file_info.get('name')
            if not name:
                continue
            file_path = Path(download_dir) / name
            try:
                st = file_path.stat()
            except (OSError, PermissionError):
                continue
            if not file_path.is_file():
                continue
            file_keys[str(file_path)] = (st.st_dev, st.st_ino)
            total_size += st.st_size

        seeding_seconds = get(raw, 'seconds_seeding')
        if (
            not isinstance(seeding_seconds, (int, float))
            or isinstance(seeding_seconds, bool)
            or (isinstance(seeding_seconds, float) and not math.isfinite(seeding_seconds))
            or seeding_seconds < 0
        ):
            seeding_seconds = None

        added_date = None
        for attr_name in ('added_date', 'add_date', 'date_added'):
            added_date = get(raw, attr_name)
            if added_date is not None:
                break
        if added_date is not None and getattr(added_date, 'tzinfo', 1) is None:
            added_date = added_date.replace(tzinfo=timezone.utc)

        seed_count = 0
        for tracker_stat in get(raw, 'tracker_stats', None) or []:
            val = getattr(tracker_stat, 'seeder_count', None)
            if isinstance(val, int) and val > 0:
                seed_count = max(seed_count, val)
        if seed_count == 0:
            val = get(raw, 'peers_sending_to_us', None)
            if isinstance(val, int) and val >= 0:
                seed_count = val

        percent_done = get(raw, 'percent_done', None)
        return {
            'id': torrent_id,
            'name': get(raw, 'name', '<unnamed torrent>'),
            'download_dir': download_dir,
            'status': str(get(raw, 'status', '')),
            'is_private': get(raw, 'is_private'),
            'seeding_time_seconds': seeding_seconds,
            'complete': percent_done is None or percent_done >= 1,
            'added_date': added_date,
            'peer_count': seed_count,
            'files_expected': len(files),
            'file_keys': file_keys,
            'total_size': total_size,
            'managed': self.in_torrent_directories(Path(download_dir)),
        }

    def _scan_library(self) -> Set[Tuple[int, int]]:
        logging.info("Building inode index for target directories: %s",
                     ', '.join(str(d) for d in self.target_directories))
        inodes: Set[Tuple[int, int]] = set()
        for target_dir in self.target_directories:
            for file_path in target_dir.rglob('*'):
                try:
                    if file_path.is_file():
                        st = file_path.stat()
                        inodes.add((st.st_dev, st.st_ino))
                except (OSError, PermissionError):
                    continue
        logging.debug("Library index: %d files", len(inodes))
        return inodes

    def _build_groups(self) -> Dict[int, int]:
        parent = {torrent_id: torrent_id for torrent_id in self.torrents}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for torrent_ids in self.inode_to_torrents.values():
            ids = list(torrent_ids)
            for other in ids[1:]:
                ra, rb = find(ids[0]), find(other)
                if ra != rb:
                    parent[rb] = ra
        return {torrent_id: find(torrent_id) for torrent_id in self.torrents}

    def group_members(self, torrent_id: int) -> List[int]:
        root = self.group_of[torrent_id]
        return sorted(t for t, r in self.group_of.items() if r == root)

    def groups(self) -> Dict[int, List[int]]:
        result: Dict[int, List[int]] = {}
        for torrent_id, root in self.group_of.items():
            result.setdefault(root, []).append(torrent_id)
        return result

    def torrents_for_paths(self, paths: List[Path]) -> Set[int]:
        """Torrents owning any of these files, by inode (hardlinks) or same path."""
        found: Set[int] = set()
        for path in paths:
            try:
                st = path.stat()
            except (OSError, PermissionError):
                continue
            found |= self.inode_to_torrents.get((st.st_dev, st.st_ino), set())
        return found

    def seeding_done(self, torrent_id: int) -> bool:
        """Public torrents are always done; private ones need the threshold."""
        info = self.torrents[torrent_id]
        if not info['complete']:
            return False
        if info['is_private'] is False:
            return True
        seconds = info['seeding_time_seconds']
        return seconds is not None and seconds >= self.threshold_seconds

    def remaining_seconds(self, torrent_id: int) -> Optional[float]:
        info = self.torrents[torrent_id]
        if self.seeding_done(torrent_id):
            return 0
        if not info['complete'] or info['seeding_time_seconds'] is None:
            return None
        return self.threshold_seconds - info['seeding_time_seconds']

    def describe(self, torrent_id: int) -> str:
        """One-line summary of a torrent for logs."""
        t = self.torrents[torrent_id]
        privacy = {True: 'private', False: 'public'}.get(t['is_private'], 'privacy?')
        return (f"#{torrent_id} [{privacy}, {t['status']}, seeded {_days(t['seeding_time_seconds'])}, "
                f"{len(t['file_keys'])}/{t['files_expected']} files] {t['name']}")

    def linked_to_library(self, torrent_id: int) -> bool:
        return any(key in self.library_inodes for key in self.torrents[torrent_id]['file_keys'].values())


class TorrentAnalyzer:
    """Decides which torrent groups can be removed."""

    @staticmethod
    def _log_decisions(index: 'TorrentLinkIndex', results: List[Dict]) -> None:
        by_reason: Dict[str, int] = {}
        for result in results:
            by_reason[result['flag_reason']] = by_reason.get(result['flag_reason'], 0) + 1
        managed = sum(1 for t in index.torrents.values() if t['managed'])
        logging.info(
            "Index: %d torrents (%d in torrent directories), %d groups, %d library files",
            len(index.torrents), managed, len(index.groups()), len(index.library_inodes),
        )
        logging.info("Decisions by reason: %s", by_reason)

        for result in results:
            line = index.describe(result['torrent_id'])
            if result['should_flag']:
                logging.info(
                    "FLAG %s | reason=%s delete_data=%s size=%s group=%s",
                    line, result['flag_reason'], result.get('delete_data'),
                    _gib(result['total_size']), result['hardlinked_to_other_torrents'] or '-',
                )
            else:
                logging.debug(
                    "keep %s | reason=%s group=%s waiting_on=%s",
                    line, result['flag_reason'], result['hardlinked_to_other_torrents'] or '-',
                    result['blocking_torrents'] or '-',
                )
        for t in index.torrents.values():
            if t['managed'] and t['files_expected'] and not t['file_keys']:
                logging.warning("Torrent #%s has none of its %d files on disk: %s/%s",
                                t['id'], t['files_expected'], t['download_dir'], t['name'])

    def __init__(self, transmission_client: 'TransmissionClient', config_dirs: Dict[str, List[str]]):
        self.transmission = transmission_client
        self.check_dirs = config_dirs['check']
        self.torrent_dirs = config_dirs['torrent']

    def analyze(self, seeding_time_threshold_days: float) -> Dict:
        logging.info(
            "Grouping torrents by shared files (private seeding minimum %s days)...",
            seeding_time_threshold_days,
        )
        rpc_torrents = self.transmission.get_all_torrents()
        verification_ids = self.transmission.verify_corrupt_torrents(rpc_torrents)
        index = TorrentLinkIndex(
            self.transmission, rpc_torrents, self.check_dirs, self.torrent_dirs,
            seeding_time_threshold_days,
        )
        unavailable = {
            t['id']: t for t in self.transmission.get_unavailable_tracker_torrents(torrents=rpc_torrents)
        }

        results = []
        for root, members in index.groups().items():
            if not any(index.torrents[m]['managed'] for m in members):
                continue
            library_members = [m for m in members if index.linked_to_library(m)]
            blocking = [m for m in members if not index.seeding_done(m)]

            if library_members:
                group_flag, group_reason = False, 'group_linked_to_library'
            elif blocking:
                group_flag, group_reason = False, 'group_member_below_threshold'
            elif len(members) == 1:
                info = index.torrents[members[0]]
                if not info['file_keys']:
                    group_flag, group_reason = True, 'missing_files'
                elif info['peer_count'] > 2:
                    group_flag, group_reason = True, 'not_hardlinked_with_more_than_two_seeds'
                else:
                    group_flag, group_reason = False, 'two_or_fewer_seeds'
            else:
                group_flag, group_reason = True, 'cross_seed_group_all_seeded'

            for member in members:
                info = index.torrents[member]
                if not info['managed']:
                    continue
                result = {
                    'torrent_id': member,
                    'torrent_name': info['name'],
                    'added_date': info['added_date'],
                    'seeding_time_seconds': info['seeding_time_seconds'],
                    'is_private': info['is_private'],
                    'peer_count': info['peer_count'],
                    'hardlinked_to_target': member in library_members,
                    'hardlinked_to_other_torrents': [m for m in members if m != member],
                    'blocking_torrents': [m for m in blocking if m != member],
                    'files_checked': info['files_expected'],
                    'files_hardlinked': sum(
                        1 for key in info['file_keys'].values()
                        if len(index.inode_to_torrents.get(key, ())) > 1 or key in index.library_inodes
                    ),
                    'disk_paths': list(info['file_keys'].keys()),
                    'total_size': info['total_size'],
                    'download_dir': info['download_dir'],
                    'status': 'hardlinked_to_target' if member in library_members else group_reason,
                    'flag_reason': group_reason,
                    'should_flag': group_flag,
                    'tracker_unavailable': False,
                }
                if member in unavailable:
                    result['status'] = 'tracker_unavailable'
                    result['tracker_unavailable'] = True
                    result['tracker_unavailable_reason'] = unavailable[member]['tracker_unavailable_reason']
                    result['flag_reason'] = 'tracker_unavailable'
                    result['should_flag'] = True
                results.append(result)

        # Never delete data that a surviving torrent still points at by path
        # (hardlinks are safe: removing one link leaves the others intact).
        flagged_ids = {r['torrent_id'] for r in results if r['should_flag']}
        for result in results:
            if not result['should_flag']:
                continue
            shared = any(
                index.path_to_torrents.get(path, set()) - flagged_ids
                for path in result['disk_paths']
            )
            result['delete_data'] = not shared
            if shared:
                result['disk_paths'] = []

        self._log_decisions(index, results)

        return {
            'timestamp': datetime.now().isoformat(),
            'torrents_analyzed': len(results),
            'tracker_unavailable_torrents': sum(1 for r in results if r['tracker_unavailable']),
            'corrupt_torrents_verified': len(verification_ids),
            'seeding_time_threshold_days': seeding_time_threshold_days,
            'check_directories': [str(d) for d in self.check_dirs],
            'results': results,
        }


RUN_ID = datetime.now().strftime('%Y%m%d-%H%M%S') + f"-{os.getpid()}"


class _RunIdFilter(logging.Filter):
    def filter(self, record):
        record.run_id = RUN_ID
        return True


def setup_logging(config: Dict, default_file: str = 'torrent_checker.log') -> None:
    """
    Log to a daily-rotated file (full detail) and to the console (summary).

    logging:
      dir: logs            # relative to the working directory
      file: torrent_checker.log
      level: DEBUG         # file level
      console_level: INFO  # stdout level
      retention_days: 30
    """
    from logging.handlers import TimedRotatingFileHandler

    log_config = config.get('logging', {}) or {}
    log_dir = Path(log_config.get('dir', 'logs'))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / Path(log_config.get('file', default_file)).name

    fmt = logging.Formatter(
        '%(asctime)s %(levelname)-7s [%(run_id)s] %(message)s', '%Y-%m-%d %H:%M:%S'
    )
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', backupCount=int(log_config.get('retention_days', 30)),
        encoding='utf-8',
    )
    file_handler.setLevel(getattr(logging, str(log_config.get('level', 'DEBUG')).upper()))
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, str(log_config.get('console_level', 'INFO')).upper()))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(_RunIdFilter())
        root.addHandler(handler)
    # Third-party libraries are noisy at DEBUG (every RPC request).
    for noisy in ('urllib3', 'transmission_rpc'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.debug("Logging to %s", log_file.resolve())


def _days(seconds: Optional[float]) -> str:
    return 'n/a' if seconds is None else f"{seconds / 86400:.1f}d"


def _gib(size: int) -> str:
    return f"{size / 1024 ** 3:.2f} GiB"


def print_results(analysis: Dict, verbose: bool = False) -> None:
    """Print analysis results in a readable format."""
    print("\n" + "="*80)
    print("TRANSMISSION HARDLINK ANALYSIS REPORT")
    print("="*80)
    print(f"Timestamp: {analysis['timestamp']}")
    print(f"Torrents Analyzed: {analysis['torrents_analyzed']}")
    print(f"Tracker-unavailable torrents: {analysis.get('tracker_unavailable_torrents', 0)}")
    print(f"Corrupt torrents queued for verification: {analysis.get('corrupt_torrents_verified', 0)}")
    print(f"Private Seeding Time Threshold: {analysis.get('seeding_time_threshold_days')} days")
    check_dirs = analysis.get('check_directories', [])
    if len(check_dirs) == 1:
        print(f"Check Directory: {check_dirs[0]}")
    else:
        print(f"Check Directories ({len(check_dirs)}):")
        for directory in check_dirs:
            print(f"  - {directory}")
    print("="*80 + "\n")
    
    if not analysis['results']:
        print("No torrents to report.\n")
        return
    
    # Summary statistics
    flagged = [r for r in analysis['results'] if r.get('should_flag', False)]
    ok = [r for r in analysis['results'] if not r.get('should_flag', False)]
    
    print(f"STATISTICS")
    print(f"  Flagged torrents: {len(flagged)}")
    tracker_flagged = [r for r in flagged if r.get('tracker_unavailable', False)]
    other_flagged = [r for r in flagged if not r.get('tracker_unavailable', False)]
    print(f"  Tracker-unavailable torrents to delete: {len(tracker_flagged)}")
    print(f"  Other cleanup torrents to delete: {len(other_flagged)}")
    print(f"  OK torrents: {len(ok)}")
    print(f"  Torrents set to verify: {analysis.get('corrupt_torrents_verified', 0)}")
    print()
    
    # Sort each group alphabetically by torrent name
    ok_sorted = sorted(ok, key=lambda r: r['torrent_name'].lower())
    tracker_flagged_sorted = sorted(tracker_flagged, key=lambda r: r['torrent_name'].lower())
    other_flagged_sorted = sorted(other_flagged, key=lambda r: r['torrent_name'].lower())
    
    # OK torrent details are opt-in because large libraries can contain
    # hundreds of healthy torrents.
    if verbose and ok_sorted:
        print("✓ OK TORRENTS:")
        print("-" * 80)
        for result in ok_sorted:
            print(f"  ✓ [{result['torrent_id']}] {result['torrent_name']}")
            print(f"    Status: {result['status']}")
            if result.get('is_private') is not None:
                print(f"    Privacy: {'private' if result['is_private'] else 'public'}")
            print(f"    Added: {result['added_date']}")
            if result.get('seeding_time_seconds') is not None:
                print(f"    Seeding time: {result['seeding_time_seconds'] / 86400:.1f} days")
            print(f"    Seeds: {result['peer_count']}")
            print(f"    Files: {result['files_hardlinked']}/{result['files_checked']} hardlinked")
            if result.get('tracker_unavailable'):
                print(f"    Tracker response: {result['tracker_unavailable_reason']}")
            
            if result['hardlinked_to_target']:
                print(f"    🔗 Hardlinked to: target")
            elif result['hardlinked_to_other_torrents']:
                torrent_ids_str = ', '.join(f"#{tid}" for tid in result['hardlinked_to_other_torrents'])
                print(f"    🔗 Hardlinked to: {torrent_ids_str}")
            if result.get('blocking_torrents'):
                print(f"    ⏳ Waiting on: {', '.join(f'#{tid}' for tid in result['blocking_torrents'])}")
            
            print()
    elif ok_sorted:
        print("OK torrent details omitted (use --verbose to show them).\n")

    reason_labels = {
        'missing_files': 'torrent data is missing from disk',
        'not_hardlinked_with_more_than_two_seeds': 'not hardlinked and has more than two seeds',
        'only_hardlinked_to_long_seeded_torrents': 'only hardlinked to torrents over the seeding-time threshold',
        'hardlinked_to_public_shorter_seeded_or_unknown_torrent': 'hardlinked to a public torrent, a torrent below the seeding-time threshold, or a torrent with unknown seeding time',
        'cross_seed_group_all_seeded': 'not in the library and every torrent sharing its files has met the seeding threshold',
    }

    # Keep tracker deletions visually separate from the hardlink policy.
    if tracker_flagged_sorted:
        print("🗑️ TRACKER-UNAVAILABLE TORRENTS TO DELETE:")
        print("-" * 80)
        for result in tracker_flagged_sorted:
            print(f"  🗑️ [{result['torrent_id']}] {result['torrent_name']}")
            print(f"    Status: {result['status']}")
            print(f"    Deletion reason: tracker reported this torrent as unavailable")
            print(f"    Tracker response: {result['tracker_unavailable_reason']}")
            print()

    if other_flagged_sorted:
        print("⚠️ OTHER FLAGGED TORRENTS TO DELETE:")
        print("-" * 80)
        for result in other_flagged_sorted:
            print(f"  ⚠️ [{result['torrent_id']}] {result['torrent_name']}")
            print(f"    Status: {result['status']}")
            if result.get('is_private') is not None:
                print(f"    Privacy: {'private' if result['is_private'] else 'public'}")
            print(f"    Deletion reason: {reason_labels.get(result.get('flag_reason'), result.get('flag_reason', 'cleanup rule'))}")
            print(f"    Added: {result['added_date']}")
            if result.get('seeding_time_seconds') is not None:
                print(f"    Seeding time: {result['seeding_time_seconds'] / 86400:.1f} days")
            print(f"    Seeds: {result['peer_count']}")
            print(f"    Files: {result['files_hardlinked']}/{result['files_checked']} hardlinked")

            if result['hardlinked_to_target']:
                print(f"    🔗 Hardlinked to: target")
            elif result['hardlinked_to_other_torrents']:
                torrent_ids_str = ', '.join(f"#{tid}" for tid in result['hardlinked_to_other_torrents'])
                print(f"    🔗 Hardlinked to: {torrent_ids_str}")

            print()

    if not flagged:
        print("No torrents marked for deletion.\n")
    for result in flagged:
        if result.get('delete_data') is False:
            print(f"  ℹ️ [{result['torrent_id']}] data kept: path shared with a surviving torrent")
    
    return flagged


def calculate_disk_space_to_free(flagged: List[Dict]) -> Tuple[int, int]:
    """Return (file_count, allocated_bytes) that deletion should free."""
    disk_files = {}
    for result in flagged:
        for file_path in result.get('disk_paths', []):
            try:
                path_stat = Path(file_path).stat()
                inode_key = (path_stat.st_dev, path_stat.st_ino)
                if inode_key not in disk_files:
                    allocated_size = getattr(path_stat, 'st_blocks', 0) * 512
                    disk_files[inode_key] = {
                        'size': allocated_size or path_stat.st_size,
                        'link_count': path_stat.st_nlink,
                        'paths_to_remove': 0,
                    }
                disk_files[inode_key]['paths_to_remove'] += 1
            except (OSError, PermissionError):
                continue

    # A hardlinked inode only frees disk space when the last selected link is
    # removed. st_blocks reflects allocated disk space more closely than the
    # logical file size reported by Transmission.
    total_disk_size = sum(
        info['size'] for info in disk_files.values()
        if info['paths_to_remove'] >= info['link_count']
    )
    total_files = sum(len(result.get('disk_paths', [])) for result in flagged)
    return total_files, total_disk_size


def prompt_delete_flagged(
    transmission_client: TransmissionClient,
    flagged: List[Dict],
    verification_count: int = 0,
) -> None:
    """
    Prompt user to delete flagged torrents and their files.
    
    Shows summary with total space to be freed (considering linked torrents) and asks for confirmation.
    """
    if not flagged:
        return
    
    # Calculate statistics
    total_torrents = len(flagged)
    total_files, total_disk_size = calculate_disk_space_to_free(flagged)
    
    # Format size for display
    def format_size(size_bytes: int) -> str:
        size_value = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_value < 1024:
                return f"{size_value:.2f} {unit}"
            size_value /= 1024
        return f"{size_value:.2f} PB"
    
    print("\n" + "="*80)
    print("DELETION CONFIRMATION")
    print("="*80)
    print(f"\nSUMMARY:")
    print(f"  Total torrents to delete: {total_torrents}")
    print(f"    Tracker-unavailable: {sum(1 for r in flagged if r.get('tracker_unavailable'))}")
    print(f"    Other cleanup rules: {sum(1 for r in flagged if not r.get('tracker_unavailable'))}")
    print(f"  Torrents set to verify: {verification_count}")
    print(f"  Total files to remove: {total_files}")
    print(f"  Total disk space to free: {format_size(total_disk_size)}")
    print()
    
    response = input("Delete these torrents and their files? (N/y): ").strip().lower()
    
    if response == 'y':
        print("\nDeleting torrents...")
        delete_torrents(transmission_client, flagged)
        print("✓ Deletion complete.")
    else:
        print("\n✗ Deletion cancelled.")


def delete_torrents(transmission_client: TransmissionClient, flagged: List[Dict]) -> None:
    """
    Delete torrents and their files from Transmission.
    """
    files, freed = calculate_disk_space_to_free(flagged)
    logging.info("Deleting %d torrent(s); expected to free %s across %d file(s)",
                 len(flagged), _gib(freed), files)
    failures = 0
    for result in flagged:
        try:
            # Keep data when another surviving torrent uses the same path.
            delete_data = result.get('delete_data', True)
            transmission_client.client.remove_torrent(result['torrent_id'], delete_data=delete_data)
            logging.info(
                "Deleted torrent %s (%s): %s%s",
                result['torrent_id'], result.get('flag_reason'), result['torrent_name'],
                '' if delete_data else ' [data kept: path shared with another torrent]',
            )
        except Exception as e:
            failures += 1
            logging.error("Failed to delete torrent %s (%s): %s",
                          result['torrent_id'], result['torrent_name'], e)
            logging.debug("Traceback:", exc_info=True)
    logging.info("Deletion finished: %d removed, %d failed", len(flagged) - failures, failures)


def main():

    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check seeding torrents for hardlink status"
    )
    parser.add_argument(
        '-c', '--config',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    parser.add_argument(
        '-j', '--json',
        action='store_true',
        help='Output results as JSON'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Show details for OK torrents (omitted by default)'
    )
    
    parser.add_argument(
        '--relink',
        action='store_true',
        help='Replace duplicate copies with hardlinks this run (overrides dedupe.relink)'
    )
    parser.add_argument(
        '--no-dedupe',
        action='store_true',
        help='Skip the duplicate-copy check this run'
    )
    parser.add_argument(
        '-y', '--yes',
        action='store_true',
        help='Delete flagged torrents without prompting (for cron)'
    )

    args = parser.parse_args()
    
    try:
        # Load configuration
        config = ConfigLoader.load(args.config)
        
        # Setup logging
        setup_logging(config)
        if 'seeding_time_threshold_days' not in config:
            logging.warning(
                "'age_threshold_days' is deprecated; it is now interpreted as a seeding-time threshold"
            )
        logging.info("=== torrent_checker start (threshold %s days, %s) ===",
                     config.get('seeding_time_threshold_days', config.get('age_threshold_days')),
                     'unattended --yes' if args.yes else 'interactive/report')
        
        # Initialize clients
        trans_config = config['transmission']
        transmission = TransmissionClient(
            trans_config['url'],
            trans_config['username'],
            trans_config['password']
        )
        
        check_dirs = config.get('check_directories', [config.get('check_directory')])
        if not check_dirs or (len(check_dirs) == 1 and check_dirs[0] is None):
            raise ValueError("Must specify either check_directory or check_directories in config")
        
        torrent_dirs = config.get('torrent_directories', [config.get('torrent_directory')])
        if not torrent_dirs or (len(torrent_dirs) == 1 and torrent_dirs[0] is None):
            raise ValueError("Must specify either torrent_directory or torrent_directories in config")
        
        # Run analysis
        analyzer = TorrentAnalyzer(transmission, {'check': check_dirs, 'torrent': torrent_dirs})
        seeding_time_threshold_days = config.get(
            'seeding_time_threshold_days', config.get('age_threshold_days')
        )
        analysis = analyzer.analyze(seeding_time_threshold_days)
        
        # Output results
        if args.json:
            print(json.dumps(analysis, indent=2, default=str))
        else:
            flagged = print_results(analysis, verbose=args.verbose)
        
        # Ask about deletion (or delete unattended with --yes)
        if args.json:
            flagged = [r for r in analysis['results'] if r.get('should_flag')]
        if args.yes and flagged:
            logging.info("--yes given: deleting %d flagged torrent(s) without prompting", len(flagged))
            delete_torrents(transmission, flagged)
        elif not args.json and flagged:
            prompt_delete_flagged(
                transmission,
                flagged,
                verification_count=analysis.get('corrupt_torrents_verified', 0),
            )
        
        # Duplicate copies that could be hardlinks (after deletions, so removed
        # torrents are not considered). Report-only unless relinking is enabled.
        if not args.no_dedupe and not args.json:
            import dedupe
            try:
                dedupe.run(
                    transmission, config,
                    lambda: TorrentLinkIndex(
                        transmission, transmission.get_all_torrents(),
                        check_dirs, torrent_dirs, seeding_time_threshold_days,
                    ),
                    relink_override=True if args.relink else None,
                )
            except Exception as e:
                logging.error("dedupe failed: %s", e)
                logging.debug("Traceback:", exc_info=True)

        logging.info("=== torrent_checker done ===")
        
    except (FileNotFoundError, ValueError, Exception) as e:
        logging.error("Error: %s", e)
        logging.debug("Traceback:", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
