# Soulseekarr

![banner](resources/banner.png)

## About

Soulseekarr reads Lidarr's _**Wanted**_ and/or _**Cutoff Unmet**_ album lists and downloads them with Slskd using [pyarr](https://github.com/totaldebug/pyarr) and [slskd-api](https://github.com/bigoulours/slskd-python-api). As downloads complete in Slskd, Soulseekarr informs Lidarr to import the files.

Alternatively, Soulseekarr can operate in various standalone modes using its search, monitoring, and post-processing options described in [`config.yml`](config.yml).

## Prerequisites

### 1. [Install Lidarr](https://lidarr.audio/)

- Specify your `lidarr.api_key`, `lidarr.host_url`, and `lidarr.download_dir` in [config.yml](config.yml).
- Ensure Lidarr can see your `slskd.download_dir`. If you are running Lidarr in a container you may need to mount the directory.

### 2. [Install Slskd](https://github.com/slskd/slskd) > 0.22.2

- Specify your `slskd.host_url`, [`slskd.api_key`](https://github.com/slskd/slskd/blob/master/docs/config.md#authentication) and `slskd.download_dir` in [config.yml](config.yml).
- Ensure Slskd can see your `slskd.download_dir`. If you are running Lidarr in a container you may need to mount the directory.

## Configuration

Soulseekarr expects [`config.yml`](config.yml) in its configuration directory (e.g. `~/.config/soulseekarr/config.yml` and typically mounted at `/data` in the container). The directory Soulseekarr looks for [`config.yml`](config.yml) is configurable using [`--config-dir`](#4-command-line-options) (see [Command-line options](#4-command-line-options)).

**Priority**: command-line options > environment variables > [`config.yml`](config.yml) > built-in defaults.

Set the top-level `interval` to the number of seconds between runs, or `0` to run once and exit. It can be overridden with `LOOP_INTERVAL` or `--interval`.

### 1. Generate [`config.yml`](config.yml)

#### Option 1: Let Soulseekarr migrate a Soularr-style `config.ini` to `config.yml` (if no `config.yml` is found)

```shell
mkdir -p "$HOME/.config/soulseekarr"
cp "$HOME/.config/soularr/config.ini" "$HOME/.config/soulseekarr/config.ini"
python3 soulseekarr.py # exits after migration
```

**Note:** Not all Soulseekarr options are covered by a Soularr `config.ini`, therefore it is still recommended to inspect the `config.yml` after migration.

#### Option 2: Copy the sample [config.yml](config.yml) template to the Soulseekarr configuration directory

```shell
mkdir -p "$HOME/.config/soulseekarr"
cp config.yml "$HOME/.config/soulseekarr/config.yml"
```

### 2. Edit [`config.yml`](config.yml)

**Configuration options are covered in-depth in the [`config.yml`](config.yml) template.**
`lidarr.import_timeout` controls how long Soulseekarr waits for a Lidarr import command. A timed-out import is treated as pending and is not regrabbed during the current process, because Lidarr may still complete it.

### 3. Environment Variables

- Any `lidarr:`/`slskd:` setting can be overridden with an env var named after its YAML key, underscored and uppercased
and prefixed with the section (similar to Slskd):
  - `lidarr.api_key` -> `LIDARR_API_KEY`
  - `slskd.minimum_filename_match_ratio` -> `SLSKD_MINIMUM_FILENAME_MATCH_RATIO`
  - `interval` -> `LOOP_INTERVAL` (`SCRIPT_INTERVAL` is supported for backward compatibility)
- Multi-option environment variables use comma-separated lists (e.g. `LIDARR_ACCEPTED_FORMATS=CD,Digital Media,Vinyl`).

### 4. Command-line options

The following runtime options can be passed directly to `soulseekarr.py` or the container,
and control where Soulseekarr runs and how often it checks for wanted releases.

| Option | Description | Default |
| --- | --- | --- |
| `-c`, `--config-dir [PATH]` | Directory containing `config.yml` | Current working directory, or `/data` in Docker |
| `-v`, `--var-dir [PATH]` | Directory for runtime files such as the lock file and logs | Current working directory, or `/data` in container |
| `--no-lock-file` | Disable lock-file creation when running outside Docker | Lock file enabled |
| `--interval SECONDS` | Override `interval` and loop forever; `0` runs once | `LOOP_INTERVAL`, then legacy `SCRIPT_INTERVAL`, then `config.yml`, then `300` in Docker or one run locally |

Example:

```bash
python soulseekarr.py --config-dir /etc/soulseekarr --var-dir /var/lib/soulseekarr --interval 300
```

## Running Soulseekarr

This repository contains sample [Quadlet](soulseekarr.container), [Compose](docker-compose.yml), and [Dockerfile](Dockerfile) files to run Soulseekarr in various ways.

The commands below assume you are in the cloned Soulseekarr program directory.

### Python virtualenv

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python soulseekarr.py [OPTION...]
```

---

### Container

Images are available via [**ghcr.io**](https://github.com/cryobry/soulseekarr/pkgs/container/soulseekarr) and [**docker.io**](https://hub.docker.com/r/cryobry/soulseekarr).

```shell
podman run -d \
  --name soulseekarr \
  --restart unless-stopped \
  --hostname soulseekarr \
  -e TZ=UTC \
  -v /media/slskd_downloads:/downloads:z \
  -v $HOME/.config/soulseekarr:/data:Z \
  docker.io/cryobry/soulseekarr:latest [OPTION...]
```

---

### [Quadlet](soulseekarr.container) containerized service (recommended)

```shell
mkdir -p ~/.config/containers/systemd
cp soulseekarr.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now soulseekarr.service
```

---

### [Compose](docker-compose.yml) (`lidarr`, `slskd`, and `soulseekarr`)

```shell
podman compose up -d
```

## Logging

Basic logging options are available in [`config.yml`](config.yml). The log file is written to the data directory (the same directory as [`config.yml`](config.yml) when running locally, or `/data/` in Docker).

When the file reaches `max_bytes`, it is rotated, keeping up to `backup_count` old files (`soulseekarr.log`, `soulseekarr.log.1`, `soulseekarr.log.2`, etc.).

### Only show the message and omit other detailed information

```yaml
logging:
  format: "%(message)s"
```

### Log to a file

  ```yaml
  logging:
    log_to_file: True
    log_file: soulseekarr.log
    max_bytes: 1048576
    backup_count: 3
  ```

See the [Python logging documentation](https://docs.python.org/3/library/logging.html) for advanced logging usage.

## Soulseekarr vs. Soularr

Soulseekarr began as a fork of [mrusse/soularr](https://github.com/mrusse/soularr), but most of its search, matching, configuration, state-management, download-monitoring, and Lidarr-integration code has been rewritten.

### Improvements

#### Per-list YAML configuration
  
The `missing` and `cutoff_unmet`Lidarr lists can override the default Lidarr and slskd settings independently. Different lists can therefore use different formats, regions, sorting methods, search intervals, matching thresholds, and download behavior.

#### Flexible sorting and randomization

Wanted albums can follow Lidarr sorting, be shuffled within each source list, or be shuffled together across all enabled lists.

#### Fewer Lidarr API requests

Soulseekarr retrieves wanted records in larger paginated batches, retains the remaining albums between processing cycles, deduplicates albums across lists, and reuses album and release information where possible.

#### Faster searching
  
Search requests are submitted continuously at the configured `minimum_search_interval` instead of waiting for each search to finish before starting the next one. Multiple searches can remain queued in slskd while its available search slots stay occupied. Search states are checked in bulk, and completed searches are collected and removed promptly.

#### Better search matching

Search supports fuzzy matching so bonus albums and other similar matches aren't skipped.

#### Improved queue control

Configurable chunk sizes determine how many albums are processed per cycle. Soulseekarr can filter unwanted files, enforce file-count safety limits, handle multi-disc releases, require all expected tracks to be accepted, and continue processing new Lidarr batches when download monitoring is disabled.

#### Album-aware download handling

Downloads are monitored as complete albums rather than unrelated transfers. Soulseekarr can detect missing statuses, stalled downloads, remote-queue timeouts, rejected transfers, hard failures, incomplete optional files, and partially accepted enqueue requests.

#### Completed-download processing

Successful albums can be renamed, consolidated from multiple source directories, tagged with album and disc metadata, and submitted to Lidarr for import. Failed imports can be moved aside and temporarily denylisted, while completed slskd transfer records can be retained or removed according to configuration.

## Additional Info

Find Soulseekarr useful? [Paypal me a coffee!](https://paypal.me/bryanroessler)

[↓ ↓ ↓ Bitcoin ↓ ↓ ↓](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)

[![Bitcoin](https://repos.bryanroessler.com/files/bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a.png)](bitcoin:bc1q7wy0kszjavgcrvkxdg7mf3s6rh506rasnhfa4a)
