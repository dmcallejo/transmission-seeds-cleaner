# Transmission Seeds Cleaner - Development Instructions

This project implements a Python utility for monitoring Transmission torrent hardlink status.

## Project Overview

- **Language**: Python 3.8+
- **Purpose**: Remove torrents (and gate Maintainerr deletions) only once every torrent sharing the same files has met the private-tracker seeding threshold
- **Key Components**: Transmission RPC client integration, hardlink detection, configuration management

## Development Guidelines

### Adding New Features

When adding features:
1. Update requirements.txt if new dependencies are needed
2. Add corresponding configuration options to config.yaml
3. Update README.md with new usage information
4. Maintain the class-based structure for modularity

### Code Organization

- `ConfigLoader`: Handles YAML configuration loading and validation
- `TransmissionClient`: Wraps Transmission RPC functionality
- `TorrentLinkIndex`: Exact per-torrent file lists (from Transmission, never folder scans), inode index, and union-find groups of torrents sharing files; seeding-done checks
- `TorrentAnalyzer`: Applies the group cleanup rules and produces reports
- `maintainerr_gate.py`: Postpones due Maintainerr items while their torrent group still has to seed
- `setup_logging`: Daily-rotated log files in `logs/` with a per-run ID

Always reason about groups: never delete data that another kept torrent uses, and never treat a torrent as unlinked just because its partner is outside the analysed set.

### Testing

Run the application with a test configuration file pointing to test directories.

## Known Limitations

- Hardlink detection is filesystem-dependent (requires inode support)
- Large directories may take time to scan
- Requires file system read access to all torrent directories
