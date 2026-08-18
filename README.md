# Transmission Seeds Cleaner

A Python utility for identifying seeding torrents in Transmission that are older than a specified threshold and checking if their files are hardlinked to a target directory or other torrents.

## Features

- Connects to a Transmission instance via RPC (supports HTTP and HTTPS)
- Identifies seeding torrents older than a configurable threshold
- Detects torrents whose tracker definitively reports them as unregistered
- Checks if torrent files are hardlinked to target directories
- Detects hardlinks between torrents and identifies relationship chains
- Intelligent flagging logic: only flags torrents that need cleanup
- Interactive torrent deletion with confirmation
- Accurate disk space calculation accounting for hardlinked files
- Generates detailed reports in both human-readable and JSON formats
- Comprehensive logging with timestamps
- Support for multiple torrent and target directories

## Requirements

- Python 3.8+
- Access to a Transmission instance with RPC enabled
- `transmission-rpc` Python package (see requirements.txt)

## Installation

1. Clone or download this repository
2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Edit `config.yaml` with your settings:

```yaml
transmission:
  url: "https://your-seedbox.com/transmission/"  # Supports HTTP and HTTPS
  username: "your-username"
  password: "your-password"

# Age threshold in days - torrents older than this will be checked
age_threshold_days: 60

# Directories where torrents are stored (used to FILTER which torrents to analyze)
torrent_directories:
  - "/mnt/storage/torrents"

# Directories to check for hardlinks (where to verify files are hardlinked)
check_directories:
  - "/mnt/storage/Movies"
  - "/mnt/storage/TV"

# Logging configuration
logging:
  level: "INFO"
  file: "torrent_checker.log"
```

Alternatively, you can use the legacy single directory format:

```yaml
check_directory: "/path/to/download/directory"
torrent_directory: "/path/to/torrent/storage"
```

### Configuration Parameters

- **transmission.url**: URL to your Transmission RPC interface (supports HTTP and HTTPS with standard ports 80/443)
- **transmission.username**: Transmission username
- **transmission.password**: Transmission password
- **age_threshold_days**: Number of days; torrents added before this threshold will be checked
- **torrent_directories**: List of directories where torrents are stored. Only torrents in these directories will be analyzed
- **torrent_directory** (legacy): Single directory where torrents are stored
- **check_directories**: List of directories to check for hardlinks. These are the target directories where hardlinked files should exist
- **check_directory** (legacy): Single directory to check for hardlinks
- **logging.level**: Logging verbosity level (DEBUG, INFO, WARNING, ERROR)
- **logging.file**: Path to log file

## Usage

Run the checker with default configuration file:

```bash
python torrent_checker.py
```

Specify a custom configuration file:

```bash
python torrent_checker.py -c /path/to/custom_config.yaml
```

Output results as JSON:

```bash
python torrent_checker.py --json
```

Show details for healthy torrents as well as flagged torrents:

```bash
python torrent_checker.py --verbose
```

## Output

### Human-Readable Report

The tool produces a detailed report showing:
- **OK torrents** (✓): Properly hardlinked to target directories or linked to younger torrents
- **Tracker-unavailable torrents to delete** (🗑️): The tracker explicitly reports that the torrent is no longer registered
- **Other flagged torrents to delete** (⚠️): Candidates selected by the hardlink cleanup rules, with the deletion reason shown
- Hardlink status with torrent relationships
- Seed count from tracker
- File hardlink statistics

After analysis, you'll be prompted to optionally delete flagged torrents with:
- Summary of torrents and files to be removed
- **Actual disk space calculation** based on files and hardlink counts currently present on disk
- Confirmation prompt before any deletion

### JSON Output

Use `--json` flag to get machine-readable JSON output suitable for scripting:

```bash
python torrent_checker.py --json > report.json
```

## Understanding the Status Values

- **hardlinked_to_target**: Files are hardlinked into the target check directory
- **hardlinked_to_other_torrents**: Files are hardlinked to other locations (likely other torrents)
- **not_hardlinked**: Files have no hardlinks; they are unique to this torrent
- **directory_not_found**: Torrent download directory doesn't exist
- **no_files_found**: No files found in the torrent directory
- **tracker_unavailable**: All reported trackers definitively rejected the torrent; it is eligible for deletion

## Flagged Torrents

Torrents are flagged (⚠️) if:
1. They are NOT hardlinked to the target directory AND
2. AND either:
   - They have NO hardlinks at all, AND have more than 2 seeds
   - OR all hardlinks are ONLY to other torrents that are also older than the threshold

Torrents are OK (✓) if:
- They ARE hardlinked to target directories
- OR they are hardlinked to other torrents that are YOUNGER than the threshold (still being actively downloaded)

This intelligent logic avoids deleting torrents that are still being used as sources for younger downloads.

Torrents that are no longer registered at their tracker are also flagged, regardless of age or seeding status. Temporary tracker errors such as timeouts or connection failures do not qualify, and a successful response from any tracker keeps the torrent out of this category. These torrents are limited to the configured torrent directories and are removed from Transmission with their local data after confirmation.

Hardlinked torrents can still be flagged when they are linked only to torrents older than the configured threshold. The report labels these separately as `only_hardlinked_to_old_torrents`; this is part of the existing cleanup policy and is distinct from tracker-unavailable deletion.

## Logging

By default, logs are written to `torrent_checker.log` and printed to stdout. Adjust the logging level in `config.yaml` for more or less verbose output:

- **DEBUG**: Very detailed information for troubleshooting
- **INFO**: General information about progress (default)
- **WARNING**: Only important notices
- **ERROR**: Only errors

## Troubleshooting

### Connection Issues

- Verify Transmission RPC is enabled in Transmission settings
- Check credentials in config.yaml
- Ensure the URL and port are correct

### Permission Errors

- The user running this script must have read access to torrent download directories
- Use `sudo` if necessary, but ensure file permissions allow it

### No Torrents Found

- Check that torrents are actually seeding
- Verify `age_threshold_days` is set appropriately
- Check Transmission logs for issues
