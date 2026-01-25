#!/usr/bin/env python3
"""
Transmission Torrent Hardlink Checker

Identifies seeding torrents older than a specified threshold and checks
if their files are hardlinked into a target directory or other torrents.
"""

import os
import sys
import json
import logging
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
    
    def get_seeding_torrents(self, older_than_days: int) -> List[Dict]:
        """
        Get all seeding torrents older than specified days.
        
        Returns:
            List of torrent dictionaries with id, name, added_date, files, and peer_count.
        """
        threshold_time = datetime.now(timezone.utc) - timedelta(days=older_than_days)
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
    
    def _get_torrent_files(self, torrent) -> List[Dict]:
        """Extract file information from a torrent."""
        files = []
        try:
            # Try different possible ways to access files
            if hasattr(torrent, 'files') and callable(torrent.files):
                file_dict = torrent.files()
                for file_obj in file_dict.values():
                    files.append({
                        'name': file_obj['name'],
                        'size': file_obj['size']
                    })
            elif hasattr(torrent, 'files'):
                # files might be a direct attribute
                file_list = torrent.files
                if isinstance(file_list, dict):
                    for file_obj in file_list.values():
                        files.append({
                            'name': file_obj['name'],
                            'size': file_obj['size']
                        })
                elif isinstance(file_list, list):
                    for file_obj in file_list:
                        files.append({
                            'name': file_obj.get('name', file_obj),
                            'size': file_obj.get('size', 0)
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
        
        # Track analyzed torrents for age validation
        self._analyzed_torrents: Dict[int, Dict] = {}  # inode -> torrent info
    
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
            'status': 'unknown',
            'should_flag': False
        }
        
        # Try to find torrent - could be a directory or a single file
        torrent_path = Path(torrent['download_dir']) / torrent['name']
        
        # Check if it's a file (single-file torrent)
        if torrent_path.is_file():
            torrent_files = [torrent_path]
        elif torrent_path.is_dir():
            # It's a directory, get all files in it
            torrent_files = self._get_file_paths(torrent_path)
        else:
            # Try directly in download_dir
            torrent_path = Path(torrent['download_dir'])
            if torrent_path.is_dir():
                torrent_files = self._get_file_paths(torrent_path)
            else:
                result['status'] = 'directory_not_found'
                result['should_flag'] = True
                return result
        
        if not torrent_files:
            logging.warning(f"[Torrent {torrent_id}] {torrent['name']}: No files found on disk")
            result['status'] = 'no_files_found'
            result['should_flag'] = True
            return result
        
        result['files_checked'] = len(torrent_files)
        
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
                        
                        # Find which torrent IDs these links belong to
                        for link_path in other_links:
                            # Try to match against known torrents to get their IDs
                            for cached_inode, cached_torrent in self._analyzed_torrents.items():
                                if cached_inode == inode:
                                    torrent_id_linked = cached_torrent['torrent_id']
                                    if torrent_id_linked != torrent_id:  # Don't include self
                                        hardlinked_torrent_ids.add(torrent_id_linked)
            
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
        try:
            for target_dir in self.target_directories:
                for item in target_dir.rglob('*'):
                    if item.is_file() and item != file_path:
                        try:
                            if item.stat().st_ino == inode:
                                return True
                        except (OSError, PermissionError):
                            continue
            return False
        except PermissionError:
            logging.warning(f"Permission denied accessing target directories")
        
        return False
    
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
        
        return results


class TorrentAnalyzer:
    """Analyzes torrent hardlink status and produces reports."""
    
    def __init__(self, transmission_client: TransmissionClient, hardlink_checker: HardlinkChecker):
        """Initialize analyzer."""
        self.transmission = transmission_client
        self.hardlink_checker = hardlink_checker
    
    def analyze(self, age_threshold_days: int) -> Dict:
        """
        Analyze all old seeding torrents for hardlink status.
        
        Returns:
            Analysis results dictionary.
        """
        logging.info(f"Fetching seeding torrents older than {age_threshold_days} days...")
        all_torrents = self.transmission.get_seeding_torrents(age_threshold_days)
        
        # Filter torrents to only those in torrent directories
        torrents = [t for t in all_torrents if self.hardlink_checker._is_in_torrent_directories(Path(t['download_dir']))]
        
        if torrents:
            logging.info(f"Found {len(torrents)} old seeding torrent(s) in torrent directories. Checking hardlinks...")
            if len(torrents) < len(all_torrents):
                logging.info(f"  (Skipped {len(all_torrents) - len(torrents)} torrent(s) not in torrent directories)")
        else:
            logging.info("No old seeding torrents found in torrent directories.")
            return {
                'timestamp': datetime.now().isoformat(),
                'torrents_analyzed': 0,
                'results': []
            }
        
        results = []
        threshold_time = datetime.now(timezone.utc) - timedelta(days=age_threshold_days)
        
        for torrent in torrents:
            logging.info(f"Checking torrent: {torrent['name']}")
            result = self.hardlink_checker.check_torrent_hardlinks(torrent)
            result['download_dir'] = torrent['download_dir']  # Add for validation
            results.append(result)
        
        # Validate hardlink ages to finalize flagging decisions
        results = self.hardlink_checker.validate_hardlink_age(results, threshold_time)
        
        return {
            'timestamp': datetime.now().isoformat(),
            'torrents_analyzed': len(torrents),
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


def print_results(analysis: Dict) -> None:
    """Print analysis results in a readable format."""
    print("\n" + "="*80)
    print("TRANSMISSION HARDLINK ANALYSIS REPORT")
    print("="*80)
    print(f"Timestamp: {analysis['timestamp']}")
    print(f"Torrents Analyzed: {analysis['torrents_analyzed']}")
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
    print(f"  OK torrents: {len(ok)}")
    print()
    
    # Sort each group alphabetically by torrent name
    ok_sorted = sorted(ok, key=lambda r: r['torrent_name'].lower())
    flagged_sorted = sorted(flagged, key=lambda r: r['torrent_name'].lower())
    
    # Print OK torrents first
    if ok_sorted:
        print("✓ OK TORRENTS:")
        print("-" * 80)
        for result in ok_sorted:
            print(f"  ✓ [{result['torrent_id']}] {result['torrent_name']}")
            print(f"    Status: {result['status']}")
            print(f"    Added: {result['added_date']}")
            print(f"    Seeds: {result['peer_count']}")
            print(f"    Files: {result['files_hardlinked']}/{result['files_checked']} hardlinked")
            
            if result['hardlinked_to_target']:
                print(f"    🔗 Hardlinked to: target")
            elif result['hardlinked_to_other_torrents']:
                torrent_ids_str = ', '.join(f"#{tid}" for tid in result['hardlinked_to_other_torrents'])
                print(f"    🔗 Hardlinked to: {torrent_ids_str}")
            
            print()
    
    # Print flagged torrents
    if flagged_sorted:
        print("⚠️ FLAGGED TORRENTS:")
        print("-" * 80)
        for result in flagged_sorted:
            print(f"  ⚠️ [{result['torrent_id']}] {result['torrent_name']}")
            print(f"    Status: {result['status']}")
            print(f"    Added: {result['added_date']}")
            print(f"    Seeds: {result['peer_count']}")
            print(f"    Files: {result['files_hardlinked']}/{result['files_checked']} hardlinked")
            
            if result['hardlinked_to_target']:
                print(f"    🔗 Hardlinked to: target")
            elif result['hardlinked_to_other_torrents']:
                torrent_ids_str = ', '.join(f"#{tid}" for tid in result['hardlinked_to_other_torrents'])
                print(f"    🔗 Hardlinked to: {torrent_ids_str}")
            
            print()
    else:
        print("⚠️ FLAGGED TORRENTS:")
        print("-" * 80)
        print("  No flagged torrents.\n")
    
    return flagged_sorted


def prompt_delete_flagged(transmission_client: TransmissionClient, flagged: List[Dict]) -> None:
    """
    Prompt user to delete flagged torrents and their files.
    
    Shows summary with total space to be freed (considering linked torrents) and asks for confirmation.
    """
    if not flagged:
        return
    
    # Calculate statistics
    total_torrents = len(flagged)
    total_files = sum(r['files_checked'] for r in flagged)
    
    # Calculate actual space to be freed, considering hardlinks
    # Group torrents by hardlink relationships to avoid counting shared space multiple times
    flagged_ids = {r['torrent_id'] for r in flagged}
    processed_ids = set()
    total_disk_size = 0
    
    for result in flagged:
        torrent_id = result['torrent_id']
        
        # Skip if we already counted this torrent as part of a hardlink group
        if torrent_id in processed_ids:
            continue
        
        torrent_size = result.get('total_size', 0)
        
        # Find all flagged torrents hardlinked to this one (including self)
        hardlinked_group = {torrent_id}
        hardlinked_group.update(
            linked_id for linked_id in result['hardlinked_to_other_torrents']
            if linked_id in flagged_ids
        )
        
        # Count this size once for the entire hardlink group
        total_disk_size += torrent_size
        
        # Mark all torrents in this group as processed
        processed_ids.update(hardlinked_group)
    
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
            flagged = print_results(analysis)
        
        # Ask about deletion
        if not args.json and flagged:
            prompt_delete_flagged(transmission, flagged)
        
        logging.info("Analysis complete.")
        
    except (FileNotFoundError, ValueError, Exception) as e:
        logging.error(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
