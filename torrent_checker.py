#!/usr/bin/env python3
"""
Transmission Torrent Hardlink Checker

Identifies seeding torrents older than a specified threshold, checks if their
files are hardlinked into a target directory or other torrents, and finds
torrents that are no longer registered at their tracker.
"""

import os
import sys
import json
import logging
import re
from datetime import datetime, timedelta, timezone
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
        required_keys = ['transmission', 'age_threshold_days']
        for key in required_keys:
            if key not in config:
                raise ValueError(f"Missing required configuration key: {key}")
        
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
    
    def get_seeding_torrents(
        self, older_than_days: int, torrents: Optional[List] = None
    ) -> List[Dict]:
        """
        Get all seeding torrents older than specified days.
        
        Returns:
            List of torrent dictionaries with id, name, added_date, files, and peer_count.
        """
        threshold_time = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        if torrents is None:
            torrents = self.client.get_torrents()
        
        old_seeding = []
        for torrent in torrents:
            # Check if torrent is seeding
            if torrent.status != 'seeding':
                continue
            
            # Get the added date using different possible attribute names
            added_date = None
            for attr_name in ['addedDate', 'added_date', 'add_date', 'date_added']:
                if hasattr(torrent, attr_name):
                    added_date = getattr(torrent, attr_name)
                    break
            
            if added_date is None:
                logging.warning(f"Could not determine added date for torrent {torrent.name}")
                continue
            
            # Ensure added_date is timezone-aware for comparison
            if added_date.tzinfo is None:
                added_date = added_date.replace(tzinfo=timezone.utc)
            
            # Check if torrent is old enough
            if added_date < threshold_time:
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
                
                old_seeding.append({
                    'id': torrent.id,
                    'name': torrent.name,
                    'added_date': added_date,
                    'files': self._get_torrent_files(torrent),
                    'download_dir': torrent.download_dir,
                    'peer_count': seed_count,
                    'torrent_obj': torrent  # Keep reference for seed count lookup
                })
        
        return old_seeding

    @staticmethod
    def _get_attr(obj, name: str, default=None):
        """Read an attribute from RPC objects and dicts used by test doubles."""
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            camel_name = re.sub(r'_([a-z])', lambda match: match.group(1).upper(), name)
            return obj.get(camel_name, default)
        try:
            return getattr(obj, name, default)
        except (AttributeError, KeyError, TypeError):
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


class HardlinkChecker:
    """Checks if files are hardlinked between torrents and directories."""
    
    def __init__(self, target_directories: List[str], torrent_directories: List[str]):
        """Initialize hardlink checker with target and torrent directories."""
        self.target_directories = []
        for target_dir in target_directories:
            target_path = Path(target_dir)
            if not target_path.exists():
                raise ValueError(f"Target directory does not exist: {target_dir}")
            self.target_directories.append(target_path)
        
        self.torrent_directories = []
        for torrent_dir in torrent_directories:
            torrent_path = Path(torrent_dir)
            if not torrent_path.exists():
                raise ValueError(f"Torrent directory does not exist: {torrent_dir}")
            self.torrent_directories.append(torrent_path)
        
        # Build inode cache for all files in torrent directories
        self._inode_cache: Dict[int, List[Path]] = {}
        self._build_inode_cache()

        # Build the target index once. The previous implementation recursively
        # scanned every target directory for every torrent file, which made
        # runtime grow as (torrent files × target files).
        self._target_inode_cache: Dict[int, List[Path]] = {}
        self._build_target_inode_cache()
        
        # Map each inode to the analyzed torrent IDs that contain it.
        self._analyzed_torrents: Dict[int, Set[int]] = {}

    def get_torrent_file_paths(self, torrent: Dict) -> List[Path]:
        """Return the local files belonging to a torrent that currently exist."""
        torrent_path = Path(torrent['download_dir']) / torrent['name']
        if torrent_path.is_file():
            return [torrent_path]
        if torrent_path.is_dir():
            return self._get_file_paths(torrent_path)

        # Use Transmission's file list when the expected root path is not
        # present. This handles single-file torrents and avoids scanning an
        # unrelated parent directory.
        paths = []
        for file_info in torrent.get('files', []):
            file_name = file_info.get('name') if isinstance(file_info, dict) else None
            if not file_name:
                continue
            file_path = Path(torrent['download_dir']) / file_name
            if file_path.is_file():
                paths.append(file_path)
        return paths
    
    def check_torrent_hardlinks(self, torrent: Dict) -> Dict:
        """
        Check if torrent files are hardlinked to target directory.
        
        Flagging rules:
        - Flag if NOT linked to target directories
        - Don't flag if linked to other torrents AND any linked torrents are younger than threshold
        
        Returns:
            Dictionary with hardlink status information.
        """
        torrent_id = torrent['id']
        result = {
            'torrent_id': torrent_id,
            'torrent_name': torrent['name'],
            'added_date': torrent['added_date'],
            'peer_count': torrent.get('peer_count', 0),
            'hardlinked_to_target': False,
            'hardlinked_to_other_torrents': [],  # List of torrent IDs
            'files_checked': 0,
            'files_hardlinked': 0,
            'disk_paths': [],
            'status': 'unknown',
            'flag_reason': None,
            'should_flag': False
        }
        
        torrent_files = self.get_torrent_file_paths(torrent)
        if not torrent_files:
            torrent_root = Path(torrent['download_dir']) / torrent['name']
            result['status'] = 'no_files_found' if torrent_root.is_dir() else 'directory_not_found'
            result['flag_reason'] = 'missing_files'
            result['should_flag'] = True
            return result
        
        result['files_checked'] = len(torrent_files)
        result['disk_paths'] = [str(file_path) for file_path in torrent_files]
        
        # Check hardlinks for each file
        hardlinked_count = 0
        hardlinked_to_target = False
        hardlinked_torrent_ids = set()
        
        for file_path in torrent_files:
            try:
                stat = file_path.stat()
                inode = stat.st_ino
                
                # Check if hardlinked to any target directory
                target_hardlink = self._has_hardlink_in_directory(file_path, inode)
                if target_hardlink:
                    hardlinked_to_target = True
                    hardlinked_count += 1
                else:
                    # Get other hardlinks (if any) from inode cache
                    other_links = self._find_hardlinks(inode, exclude_path=file_path)
                    if other_links:
                        hardlinked_count += 1
                        
                        # Find which analyzed torrents these links belong to.
                        hardlinked_torrent_ids.update(
                            linked_id for linked_id in self._analyzed_torrents.get(inode, set())
                            if linked_id != torrent_id
                        )

                self._analyzed_torrents.setdefault(inode, set()).add(torrent_id)
            
            except (OSError, PermissionError) as e:
                logging.warning(f"[Torrent {torrent_id}] Could not check file {file_path.name}: {e}")
        
        result['files_hardlinked'] = hardlinked_count
        result['hardlinked_to_target'] = hardlinked_to_target
        result['hardlinked_to_other_torrents'] = sorted(list(hardlinked_torrent_ids))
        
        # Calculate total size of torrent files
        total_size = 0
        try:
            for file_path in torrent_files:
                try:
                    total_size += file_path.stat().st_size
                except (OSError, PermissionError):
                    pass
        except Exception:
            pass
        result['total_size'] = total_size
        
        # Determine status and whether to flag
        if hardlinked_to_target:
            result['status'] = 'hardlinked_to_target'
            result['should_flag'] = False  # Rule 1: Linked to target = OK
        elif hardlinked_torrent_ids:
            result['status'] = 'hardlinked_to_other_torrents'
            # Rule 2: Check if any linked torrents are younger than threshold
            # Will be evaluated after all torrents are analyzed
            result['should_flag'] = None  # To be determined after full analysis
        else:
            result['status'] = 'not_hardlinked'
            # Rule 3: Only flag if peer count > 2
            peer_count = result['peer_count']
            result['should_flag'] = isinstance(peer_count, int) and peer_count > 2
            if result['should_flag']:
                result['flag_reason'] = 'not_hardlinked_with_more_than_two_seeds'
        
        return result
    
    def _get_file_paths(self, directory: Path) -> List[Path]:
        """Recursively get all file paths in directory."""
        files = []
        try:
            for item in directory.rglob('*'):
                if item.is_file():
                    files.append(item)
        except PermissionError as e:
            logging.warning(f"Permission denied accessing {directory}: {e}")
        except Exception as e:
            logging.warning(f"Error scanning directory {directory}: {e}")
        
        return files
    
    def _has_hardlink_in_directory(self, file_path: Path, inode: int) -> bool:
        """Check if file has a hardlink in any of the target directories."""
        return any(item != file_path for item in self._target_inode_cache.get(inode, []))
    
    def _find_hardlinks(self, inode: int, exclude_path: Path) -> List[Path]:
        """Find all hardlinks for a given inode (excluding the original path)."""
        if inode not in self._inode_cache:
            return []
        
        return [p for p in self._inode_cache[inode] if p != exclude_path]
    
    def _build_inode_cache(self) -> None:
        """Build a cache of inodes to file paths for all files in torrent directories."""
        logging.info("Building inode cache for torrent directories...")
        for torrent_dir in self.torrent_directories:
            try:
                for file_path in torrent_dir.rglob('*'):
                    if file_path.is_file():
                        try:
                            inode = file_path.stat().st_ino
                            if inode not in self._inode_cache:
                                self._inode_cache[inode] = []
                            self._inode_cache[inode].append(file_path)
                        except (OSError, PermissionError):
                            continue
            except PermissionError as e:
                logging.warning(f"Permission denied accessing {torrent_dir}: {e}")
            except Exception as e:
                logging.warning(f"Error scanning {torrent_dir}: {e}")

    def _build_target_inode_cache(self) -> None:
        """Build an inode-to-path index for all target-directory files."""
        logging.info("Building inode cache for target directories...")
        for target_dir in self.target_directories:
            try:
                for file_path in target_dir.rglob('*'):
                    if file_path.is_file():
                        try:
                            inode = file_path.stat().st_ino
                            self._target_inode_cache.setdefault(inode, []).append(file_path)
                        except (OSError, PermissionError):
                            continue
            except PermissionError as e:
                logging.warning(f"Permission denied accessing {target_dir}: {e}")
            except Exception as e:
                logging.warning(f"Error scanning {target_dir}: {e}")
    
    def _is_in_torrent_directories(self, path: Path) -> bool:
        """Check if a path is within any torrent directory."""
        try:
            path = path.resolve()
            for torrent_dir in self.torrent_directories:
                try:
                    # Check if path is relative to torrent_dir (i.e., it's within it)
                    path.relative_to(torrent_dir)
                    return True
                except ValueError:
                    # path is not relative to torrent_dir
                    continue
        except Exception:
            pass
        return False
    
    def validate_hardlink_age(self, results: List[Dict], age_threshold: datetime) -> List[Dict]:
        """
        Validate hardlinked torrents to apply correct flagging rules.
        
        Rules:
        - OK if hardlinked to target directories
        - OK if hardlinked to other torrents AND any of those torrents are younger than threshold
        - FLAG if NOT linked to target AND (not linked at all OR all linked torrents are older than threshold)
        """
        # Build a map of torrent ID to added date
        torrent_by_id = {r['torrent_id']: r['added_date'] for r in results}
        
        # Check each result that has pending flag validation
        for result in results:
            if result['should_flag'] is None:  # Pending determination
                # This torrent is hardlinked to others - check if ANY are younger than threshold
                # If any linked torrent is younger, it's OK (don't flag)
                # If all linked torrents are older, it's FLAG
                
                any_linked_younger = False
                
                # Check the linked torrent IDs directly from the result
                for linked_torrent_id in result['hardlinked_to_other_torrents']:
                    if linked_torrent_id in torrent_by_id:
                        linked_added_date = torrent_by_id[linked_torrent_id]
                        # If ANY linked torrent is younger than threshold, it's OK
                        if linked_added_date > age_threshold:
                            any_linked_younger = True
                            break
                
                # Flag only if NO linked torrents are younger (i.e., all are old)
                result['should_flag'] = not any_linked_younger
                result['flag_reason'] = (
                    'only_hardlinked_to_old_torrents'
                    if result['should_flag']
                    else 'hardlinked_to_younger_torrent'
                )
        
        return results


class TorrentAnalyzer:
    """Analyzes torrent hardlink status and produces reports."""
    
    def __init__(self, transmission_client: TransmissionClient, hardlink_checker: HardlinkChecker):
        """Initialize analyzer."""
        self.transmission = transmission_client
        self.hardlink_checker = hardlink_checker
    
    def analyze(self, age_threshold_days: int) -> Dict:
        """
        Analyze old seeding torrents and find torrents no longer registered
        at their tracker.
        
        Returns:
            Analysis results dictionary.
        """
        logging.info(f"Fetching seeding torrents older than {age_threshold_days} days...")
        # Fetch the complete RPC snapshot once. Both the age and tracker
        # analyses operate on the same snapshot, avoiding a second large
        # torrent-get request and inconsistent results between the two scans.
        rpc_torrents = self.transmission.get_all_torrents()
        all_torrents = self.transmission.get_seeding_torrents(
            age_threshold_days, torrents=rpc_torrents
        )
        logging.info(
            "Checking tracker responses for unavailable torrents "
            "(all torrents; age threshold is not applied)..."
        )
        unavailable_torrents = self.transmission.get_unavailable_tracker_torrents(
            torrents=rpc_torrents
        )

        # Filter torrents to only those in torrent directories
        torrents = [t for t in all_torrents if self.hardlink_checker._is_in_torrent_directories(Path(t['download_dir']))]
        unavailable_torrents = [
            t for t in unavailable_torrents
            if self.hardlink_checker._is_in_torrent_directories(Path(t['download_dir']))
        ]
        
        if torrents:
            logging.info(f"Found {len(torrents)} old seeding torrent(s) in torrent directories. Checking hardlinks...")
            if len(torrents) < len(all_torrents):
                logging.info(f"  (Skipped {len(all_torrents) - len(torrents)} torrent(s) not in torrent directories)")
        else:
            logging.info("No old seeding torrents found in torrent directories.")

        if unavailable_torrents:
            logging.info(
                f"Found {len(unavailable_torrents)} torrent(s) no longer registered at their tracker(s)."
            )
        
        results = []
        threshold_time = datetime.now(timezone.utc) - timedelta(days=age_threshold_days)
        
        for torrent in torrents:
            logging.info(f"Checking torrent: {torrent['name']}")
            result = self.hardlink_checker.check_torrent_hardlinks(torrent)
            result['download_dir'] = torrent['download_dir']  # Add for validation
            results.append(result)
        
        # Validate hardlink ages to finalize flagging decisions
        results = self.hardlink_checker.validate_hardlink_age(results, threshold_time)

        # Tracker-unavailable torrents are deletion candidates regardless of
        # age or Transmission's current status. Avoid duplicate IDs when a
        # torrent is present in both analyses.
        unavailable_by_id = {torrent['id']: torrent for torrent in unavailable_torrents}
        analyzed_ids = {result['torrent_id'] for result in results}
        for result in results:
            unavailable = unavailable_by_id.get(result['torrent_id'])
            if unavailable is not None:
                result['status'] = 'tracker_unavailable'
                result['tracker_unavailable'] = True
                result['tracker_unavailable_reason'] = unavailable['tracker_unavailable_reason']
                result['flag_reason'] = 'tracker_unavailable'
                result['should_flag'] = True

        for torrent in unavailable_torrents:
            if torrent['id'] in analyzed_ids:
                continue
            results.append({
                'torrent_id': torrent['id'],
                'torrent_name': torrent['name'],
                'added_date': torrent['added_date'],
                'peer_count': torrent.get('peer_count', 0),
                'hardlinked_to_target': False,
                'hardlinked_to_other_torrents': [],
                'files_checked': len(torrent.get('files', [])),
                'files_hardlinked': 0,
                'disk_paths': [
                    str(path) for path in self.hardlink_checker.get_torrent_file_paths(torrent)
                ],
                'total_size': sum(file_info.get('size', 0) for file_info in torrent.get('files', [])),
                'status': 'tracker_unavailable',
                'tracker_unavailable': True,
                'tracker_unavailable_reason': torrent['tracker_unavailable_reason'],
                'flag_reason': 'tracker_unavailable',
                'should_flag': True,
                'download_dir': torrent['download_dir'],
            })

        return {
            'timestamp': datetime.now().isoformat(),
            'torrents_analyzed': len(results),
            'tracker_unavailable_torrents': len(unavailable_torrents),
            'age_threshold_days': age_threshold_days,
            'check_directories': [str(d) for d in self.hardlink_checker.target_directories],
            'results': results
        }


def setup_logging(config: Dict) -> None:
    """Setup logging based on configuration."""
    log_config = config.get('logging', {})
    log_level = getattr(logging, log_config.get('level', 'INFO'))
    log_file = log_config.get('file', 'torrent_checker.log')
    
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    log_datefmt = '%Y-%m-%d %H:%M:%S'
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=log_datefmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )


def print_results(analysis: Dict, verbose: bool = False) -> None:
    """Print analysis results in a readable format."""
    print("\n" + "="*80)
    print("TRANSMISSION HARDLINK ANALYSIS REPORT")
    print("="*80)
    print(f"Timestamp: {analysis['timestamp']}")
    print(f"Torrents Analyzed: {analysis['torrents_analyzed']}")
    print(f"Tracker-unavailable torrents: {analysis.get('tracker_unavailable_torrents', 0)}")
    print(f"Age Threshold: {analysis.get('age_threshold_days')} days")
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
            print(f"    Added: {result['added_date']}")
            print(f"    Seeds: {result['peer_count']}")
            print(f"    Files: {result['files_hardlinked']}/{result['files_checked']} hardlinked")
            if result.get('tracker_unavailable'):
                print(f"    Tracker response: {result['tracker_unavailable_reason']}")
            
            if result['hardlinked_to_target']:
                print(f"    🔗 Hardlinked to: target")
            elif result['hardlinked_to_other_torrents']:
                torrent_ids_str = ', '.join(f"#{tid}" for tid in result['hardlinked_to_other_torrents'])
                print(f"    🔗 Hardlinked to: {torrent_ids_str}")
            
            print()
    elif ok_sorted:
        print("OK torrent details omitted (use --verbose to show them).\n")

    reason_labels = {
        'missing_files': 'torrent data is missing from disk',
        'not_hardlinked_with_more_than_two_seeds': 'not hardlinked and has more than two seeds',
        'only_hardlinked_to_old_torrents': 'only hardlinked to torrents older than the threshold',
        'hardlinked_to_younger_torrent': 'hardlinked to a younger torrent',
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
            print(f"    Deletion reason: {reason_labels.get(result.get('flag_reason'), result.get('flag_reason', 'cleanup rule'))}")
            print(f"    Added: {result['added_date']}")
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


def prompt_delete_flagged(transmission_client: TransmissionClient, flagged: List[Dict]) -> None:
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
    for result in flagged:
        try:
            # Remove torrent with delete_data=True to also delete files
            transmission_client.client.remove_torrent(result['torrent_id'], delete_data=True)
            logging.info(f"Deleted torrent {result['torrent_id']}: {result['torrent_name']}")
        except Exception as e:
            logging.error(f"Failed to delete torrent {result['torrent_id']}: {e}")


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
    
    args = parser.parse_args()
    
    try:
        # Load configuration
        config = ConfigLoader.load(args.config)
        
        # Setup logging
        setup_logging(config)
        logging.info("Starting Transmission hardlink checker...")
        
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
        
        hardlink_checker = HardlinkChecker(check_dirs, torrent_dirs)
        
        # Run analysis
        analyzer = TorrentAnalyzer(transmission, hardlink_checker)
        analysis = analyzer.analyze(config['age_threshold_days'])
        
        # Output results
        if args.json:
            print(json.dumps(analysis, indent=2, default=str))
        else:
            flagged = print_results(analysis, verbose=args.verbose)
        
        # Ask about deletion
        if not args.json and flagged:
            prompt_delete_flagged(transmission, flagged)
        
        logging.info("Analysis complete.")
        
    except (FileNotFoundError, ValueError, Exception) as e:
        logging.error(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
