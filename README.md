
# Seekarr

![banner](resources/banner.png)

## About

Seekarr reads the "wanted" and/or "cutoff unmet" Lidarr album lists and downloads them with Slskd using the [pyarr](https://github.com/totaldebug/pyarr) and [slskd-api](https://github.com/bigoulours/slskd-python-api) Python libraries.

As downloads complete in Slskd, Seekarr informs Lidarr to import the files. Alternatively, Seekarr can operate in various standalone modes using settings in [`config.yml`](config.yml).

This is am especially great way to acquire difficult-to-find public domain recordings.

## Setup

1. [Install Lidarr](https://lidarr.audio/)

    - Specify your `lidarr.api_key`, `lidarr.host_url`, and `lidarr.download_dir` in [config.yml](config.yml).
    - Ensure Lidarr can see your `slskd.download_dir`. If you are running Lidarr in a container you may need to mount the directory.

2. [Install Slskd](https://github.com/slskd/slskd)

    - Specify your `slskd.host_url`, [`slskd.api_key`](https://github.com/slskd/slskd/blob/master/docs/config.md#authentication) and `slskd.download_dir` in [config.yml](config.yml).
    - Ensure Slskd can see your `slskd.download_dir`. If you are running Lidarr in a container you may need to mount the directory.

---

### Podman/Docker (recommended)

Soularr container images are available via [ghcr.io](https://github.com/cryobry/seekarr/pkgs/container/seekarr) and [docker.io](https://hub.docker.com/r/cryobry/seekarr).

```shell
podman run -d \
  --name seekarr \
  --restart unless-stopped \
  --hostname seekarr \
  -e TZ=UTC \
  -v /media/slskd_downloads:/downloads \
  -v /containers/seekarr:/data \
  cryobry/seekarr:latest [OPTION...] [CMD...]
```

---

### [Quadlet](seekarr.container)

```ini
[Unit]
Description=seekarr container
Requires=podman.socket
After=podman.socket

[Container]
ContainerName=seekarr
Image=docker.io/cryobry/seekarr:latest
Pull=newer
Volume=%h/.config/seekarr:/data:Z
Volume=%h/downloads/htpc:/downloads:z
Environment=SCRIPT_INTERVAL=5
Environment=TZ=America/New_York

[Service]
Restart=on-failure

[Install]
WantedBy=default.target

```

#### Run seekarr as a rootless container user service using quadlet

```bash
mkdir -p ~/.config/containers/systemd
cp seekarr.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now seekarr.service
```

---

### [Compose](docker-compose.yml) (`lidarr`, `slskd`, and `seekarr`)

```yml
services:
  lidarr:
    image: ghcr.io/hotio/lidarr:latest
    container_name: lidarr
    hostname: lidarr
    environment:
      - TZ=ETC/UTC
    volumes:
      - /containers/lidarr:/config
      - /media:/data
    ports:
      - "8686:8686"
    restart: unless-stopped

  slskd:
    image: slskd/slskd
    container_name: slskd
    hostname: slskd
    environment:
      - TZ=ETC/UTC
      - SLSKD_REMOTE_CONFIGURATION=true
    ports:
      - "5030:5030"
      - "5031:5031"
      - "50300:50300"
    volumes:
      - /containers/slskd:/app
      - /media:/data
    restart: unless-stopped

  seekarr:
    image: cryobry/seekarr:latest
    container_name: seekarr
    hostname: seekarr
    environment:
      - TZ=ETC/UTC
      - SCRIPT_INTERVAL=300
    volumes:
      - /media/slskd_downloads:/downloads
      - /container/seekarr:/data
    restart: unless-stopped
```

## Configure [`config.yml`](config.yml)

### Example

```yaml
# Defaults for both "missing" and "cutoff_unmet" lists
lidarr:
  # === API SETTINGS ===
  # Get from Lidarr: Settings > General > Security
  api_key: yourlidarrapikeygoeshere
  # URL Lidarr uses (e.g., what you use in your browser)
  host_url: http://lidarr:8686
  # Path to slskd downloads inside the Lidarr container
  download_dir: /data/slskd_downloads

  # === SEARCH SETTINGS ===
  # Which Lidarr wanted lists to pull from, and in what order
  sources:
    - missing
    - cutoff_unmet
  # Search modes: all, incrementing_page, first_page
  # "all": search for every wanted record
  # "first_page": repeatedly searches the first page
  # "incrementing_page": starts with the first page and increments on each run.
  type: incrementing_page
  # Albums to process per run
  page_size: 10
  # Blacklist words in album or track title search (case-insensitive)
  title_blacklist:
    - Word1
    - word2
  # Use the release manually selected in Lidarr, ignoring the other release settings below
  use_selected_lidarr_release: False
  # Pick release with most common track count
  use_most_common_tracknum: True
  allow_multi_disc: True
  # Accepted release countries
  accepted_countries:
    - Europe
    - Japan
    - United Kingdom
    - United States
    - "[Worldwide]"
    - Australia
    - Canada
  # Don't check the region of the release
  skip_region_check: False
  # If true, Lidarr won't auto-import from Slskd and Seekarr will skip grabbed albums
  disable_sync: False
  # Skip re-downloading albums that previously failed to import into Lidarr
  failed_import_denylist: True
  # Accepted formats to download
  accepted_formats:
    - CD
    - Digital Media
    - Vinyl

# Defaults for both "missing" and "cutoff_unmet" lists
slskd:
  # === API SETTINGS ===
  # Create manually (see docs)
  api_key: yourslskdapikeygoeshere
  # URL Slskd uses
  host_url: http://slskd:5030
  # URL path to append to host_url (ex. http://slskd:5030/slskd = /slskd)
  url_base: /
  # Download path inside Slskd container
  download_dir: /downloads

  # === SEARCH SETTINGS ===
  # Search timeout in seconds
  timeout: 10
  # Maximum number of peers allowed in the user's queue
  maximum_peer_queue: 50
  # Minimum upload speed (bits/sec)
  minimum_peer_upload_speed: 100
  # Minimum match ratio between Lidarr track and Soulseek filename
  minimum_filename_match_ratio: 0.8
  # Minimum time (seconds) between searches. Set to 0 to disable.
  minimum_search_interval: 5
  # Delete search after Seekarr runs
  delete_searches: False
  # Max seconds to wait for downloads
  stalled_timeout: 7200
  # How long to wait for a remote queue to finish before timing out
  remote_queue_timeout: 3600
  # Remove successfully completed downloads from slskd's transfer list after each run
  remove_completed_downloads: True
  # Requeue individual files that error out or get rejected during download.
  # If False, the whole album grab is marked failed as soon as one file errors, with no retry.
  requeue_failed_downloads: True
  # Prepend artist name when searching for albums
  album_prepend_artist: False
  filtering: True
  use_extension_whitelist: False
  extensions_whitelist:
    - lrc
    - nfo
    - txt
  # Blacklist words in search query (case-insensitive)
  search_blacklist:
    - WordToStripFromSearch1
    - WordToStripFromSearch2
  # Rename completed downloads to "Artist - Album (Year)" before Lidarr import
  rename_download_folders: True
  # Preferred file types and qualities (most to least preferred)
  # Use "flac" or "mp3" to ignore quality details
  allowed_filetypes:
    - flac 24/48
    - flac 24/192
    - flac 16/44.1
    - flac
    - mp3 320
    - mp3
  # Users to ignore
  ignored_users:
    - User1
    - User2

# Custom overrides for "missing" list
missing:
  lidarr:
    accepted_formats:
      - CD
      - Digital Media
      - Vinyl
  slskd:
    allowed_filetypes:
      - flac 24/96
      - flac 24/48
      - flac 24/192
      - flac 16/44.1
      - flac
      - mp3 320
      - mp3

# Custom overrides for "cutoff_unmet" list
cutoff_unmet:
  lidarr:
    accepted_formats:
      - CD
      - Digital Media
  slskd:
    allowed_filetypes:
      - flac 24/192
      - flac 16/44.1
      - flac

logging:
  # Passed to Python's logging.basicConfig()
  # See: https://docs.python.org/3/library/logging.html
  level: INFO
  format: "[%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s: %(message)s"
  datefmt: "%Y-%m-%dT%H:%M:%S%z"
  # Enable logging to a file in addition to stdout
  log_to_file: True
  # Log filename (resolved relative to the data directory)
  log_file: seekarr.log
  # Maximum log file size in bytes before rotation (default: 1MB)
  max_bytes: 1048576
  # Number of rotated log files to keep
  backup_count: 3
```

### Specify which Lidarr wanted lists to search using `lidarr.sources`

The `lidarr.sources` list specifies the order of Lidarr wanted lists to process (e.g. [`missing`, `cutoff_unmet`]).

### Per-list overrides

The nested blocks in `missing:` and `cutoff_unmet:` override the top-level `lidarr:` and `slskd:` defaults.
Useful if, say, you want to accept Vinyl releases for missing albums but only CD/Digital for cutoff-unmet upgrades.

List of [countries](https://musicbrainz.org/doc/Release/Country) and [formats](https://pastebin.com/raw/pzGVUgaE) from MusicBrainz.

### Environment Variable Overrides

Env vars always take priority over `config.yml`, including per-source (`missing:`/`cutoff_unmet:`) overrides.

Any `lidarr:`/`slskd:` setting can be overridden with an env var named after its YAML key, underscored and uppercased
and prefixed with the section (similar to Slskd):

- `lidarr.api_key` -> `LIDARR_API_KEY`
- `slskd.minimum_filename_match_ratio` -> `SLSKD_MINIMUM_FILENAME_MATCH_RATIO`

This is handy for keeping secrets like API keys out of `config.yml` (e.g. via Docker secrets).

List values are comma-separated (e.g. `LIDARR_ACCEPTED_FORMATS=CD,Digital Media,Vinyl`).

## Running Seekarr

```bash
python -m pip install -r requirements.txt
python seekarr.py
```

Note: `seekarr.py` expects `config.yml` to be in the same directory unless `--config` is specified.

### Command-line runtime options

The following options control where Seekarr runs and how often it checks for wanted releases. They are
runtime options rather than settings in `config.yml` and can be passed directly to `seekarr.py` or the container.

| Option | Description | Default |
| --- | --- | --- |
| `-c`, `--config-dir [PATH]` | Directory containing `config.yml` | Current working directory, or `/data` in Docker |
| `-v`, `--var-dir [PATH]` | Directory for runtime files such as the lock file, logs, current-page state, and failed-import denylist | Current working directory, or `/data` in Docker |
| `--no-lock-file` | Disable lock-file creation when running outside Docker | Lock file enabled |
| `--interval SECONDS` | Loop forever and wait this many seconds between runs | `SCRIPT_INTERVAL`, then `300` in Docker, otherwise one run |

The `--config-dir` and `--var-dir` options accept an optional path. When supplied without a path, they use
their default directory. For example:

```bash
python seekarr.py --config-dir /etc/seekarr --var-dir /var/lib/seekarr --interval 300
```

`--interval` takes precedence over the `SCRIPT_INTERVAL` environment variable. These runtime options take
precedence over their built-in defaults; application settings continue to follow the configuration precedence
documented above, with environment variables taking precedence over `config.yml`.

## Logging

Basic logging options are available in `config.yml`. The defaults are sensible for most logging scenarios,
but are still somewhat opinionated. Some users may not like how the log messages are formatted and would prefer
simpler output than what is provided by default.

For example, if you want the logs to only show the message and none of the other detailed information, edit the
`logging` section's `format` property to:

```yaml
logging:
  format: "%(message)s"
```

### Log to a File

Seekarr can write logs to a rotating file in addition to stdout. Enable it in your `config.yml`:

```yaml
logging:
  log_to_file: True
  log_file: seekarr.log
  max_bytes: 1048576
  backup_count: 3
```

The log file is written to the data directory (the same directory as `config.yml` when running locally, or `/data/` in Docker). When the file reaches `max_bytes`, it is rotated, keeping up to `backup_count` old files (`seekarr.log`, `seekarr.log.1`, `seekarr.log.2`, etc.).

See the [Python logging documentation](https://docs.python.org/3/library/logging.html) for advanced logging usage.

## Additional Info

Find Seekarr useful? [Paypal me a coffee!](https://paypal.me/bryanroessler)

[↓ ↓ ↓ Bitcoin ↓ ↓ ↓](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)

[![Bitcoin](https://repos.bryanroessler.com/files/bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a.png)](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)
