
# Seekarr

![banner](resources/banner.png)

## About

Seekarr reads the "wanted" and/or "cutoff unmet" album lists from Lidarr and downloads them using Slskd using the [pyarr](https://github.com/totaldebug/pyarr) and [slskd-api](https://github.com/bigoulours/slskd-python-api) libraries.

As downloads complete in Slskd, Seekarr informs Lidarr to import the files. Alternatively, Seekarr can operate in various standalone modes using the settings in [`config.yml`](config.yml).

## Setup

### [Install Lidarr](https://lidarr.audio/)

Make sure Lidarr can see your Slskd download directory. If you are running Lidarr in a container you may need to mount the directory. You will then need to add it to your config (see `lidarr.download_dir` in the [example config](config.yml)).

### [Install Slskd](https://github.com/slskd/slskd)

Seekarr requires an [api key from Slskd](https://github.com/slskd/slskd/blob/master/docs/config.md#authentication) added to the the yml file under `web, authentication, api_keys, my_api_key`).

---

### Podman/Docker (recommended)

Container images are available via [ghcr.io](https://github.com/cryobry/seekarr/pkgs/container/seekarr) and [docker.io](https://hub.docker.com/r/cryobry/seekarr).

```shell
podman run -d \
  --name seekarr \
  --restart unless-stopped \
  --hostname seekarr \
  -e TZ=ETC/UTC \
  -v /media/slskd_downloads:/downloads \
  -v /containers/seekarr:/data \
  cryobry/seekarr:latest [OPTION...] [CMD...]
```

### [Compose file](docker-compose.yml)

```yml
services:
  seekarr:
    image: cryobry/seekarr:latest
    container_name: seekarr
    hostname: seekarr
    user: 1000:1000 # set to your UID and GID, which can be determined via `id -u` and `id -g`, respectively
    environment:
      - TZ=Etc/UTC
      - SCRIPT_INTERVAL=60 # delay before reloop in seconds
    volumes:
      # /downloads should match the slskd.download_dir in config.yml
      - /media/slskd_downloads:/downloads
      # Seekarr expects the config file at "/data" by default (use --config to override)
      - /containers/seekarr:/data
    restart: unless-stopped
```

### Compose file combining `lidarr`, `slskd`, and `seekarr`

```yml
services:
  lidarr:
    image: ghcr.io/hotio/lidarr:latest
    container_name: lidarr
    hostname: lidarr
    environment:
      - TZ=ETC/UTC
      - PUID=1000
      - PGID=1000
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
    user: 1000:1000
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
    user: 1000:1000
    environment:
      - TZ=ETC/UTC
      - SCRIPT_INTERVAL=300
    volumes:
      - /media/slskd_downloads:/downloads
      - /container/seekarr:/data
    restart: unless-stopped
```

## Configure `config.yml`

Top-level `lidarr:` and `slskd:` hold both connection settings (`api_key`, `host_url`, `download_dir`, etc.) and the
default search/download behavior applied to every source. `lidarr.sources` lists which Lidarr wanted lists to process,
and in what order (e.g. `missing`, `cutoff_unmet`). A top-level block matching one of those source names (e.g.
`missing:` / `cutoff_unmet:`) can override `accepted_formats` (Lidarr release format) and `allowed_filetypes` (Slskd
quality) for that specific source only — useful if, say, you want to accept Vinyl releases for missing albums but
only CD/Digital for cutoff-unmet upgrades.

**Example [config.yml](config.yml):**

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

# Overrides for "missing" list. Only accepted_formats (lidarr) and allowed_filetypes (slskd) can
# be overridden here; everything else always comes from the top-level lidarr:/slskd: defaults.
# Omit a source block entirely (or a lidarr:/slskd: key within it) to just use the defaults.
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

# Overrides for "cutoff_unmet" list
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

List of [countries](https://musicbrainz.org/doc/Release/Country) and [formats](https://pastebin.com/raw/pzGVUgaE) from MusicBrainz.

## Running

Install the requirements and run Seekar:

```bash
python -m pip install -r requirements.txt
python seekarr.py
```

Note: the `config.yml` file needs to be in the same directory as `seekarr.py`.

## Logging

There are some very basic options for logging found under the `logging` section of the `config.yml` file. The defaults
should be sensible for a typical logging scenario, but are still somewhat opinionated. Some users may not like how the
log messages are formatted and would prefer a much simpler output than what is provided by default.

For example, if you want the logs to only show the message and none of the other detailed information, edit the
`logging` section's `format` property to look like this:

```yaml
logging:
  format: "%(message)s"
```

For more information on the options available for logging, including more options for changing how the messages are
formatted, see the comments in the `logging` section from the [example config.yml](#configure-configyml).

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

### Advanced Logging Usage

For more information on the options available for logging, including more options for changing how messages are
formatted, see the [Python logging documentation](https://docs.python.org/3/library/logging.html).

## Additional Info

Find Seekarr useful? [Paypal me a coffee!](https://paypal.me/bryanroessler)

[↓ ↓ ↓ Bitcoin ↓ ↓ ↓](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)

[![Bitcoin](https://repos.bryanroessler.com/files/bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a.png)](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)
