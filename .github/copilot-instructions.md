# Transmission Seeds Cleaner - Development Instructions

This project implements a Python utility for monitoring Transmission torrent hardlink status.

## Project Overview

- **Language**: Python 3.8+
- **Purpose**: Monitor seeding torrents older than a threshold and check hardlink status
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
- `HardlinkChecker`: Detects hardlinks between files and directories
- `TorrentAnalyzer`: Coordinates analysis and produces reports

### Testing

Run the application with a test configuration file pointing to test directories.

## Known Limitations

- Hardlink detection is filesystem-dependent (requires inode support)
- Large directories may take time to scan
- Requires file system read access to all torrent directories
