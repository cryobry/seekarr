# Soulseekarr Agent Guidelines

## Architecture

Soulseekarr keeps its runtime in the single-file Python script ([soulseekarr.py](soulseekarr.py));
legacy config migration lives in [migration.py](migration.py). Do not split runtime behavior into
additional modules unless the user explicitly asks for it.

- `LidarrConfig` / `SlskdConfig` / `AppConfig` (dataclasses) model `config.yml`. `AppConfig.from_yaml`
  merges the top-level `lidarr`/`slskd` defaults with an optional per-`source` override block
  (top-level `missing`/`cutoff_unmet` keys); `main()` builds one `source_configs[source]` entry per
  configured source, and each `Album`'s `cfg` field is bound to that source's `AppConfig` once, in
  `__post_init__`, at construction time.
- Every `lidarr.*`/`slskd.*` config value can also be overridden by an env var named
  `<SECTION>_<KEY>` (e.g. `LIDARR_API_KEY`, `SLSKD_MINIMUM_FILENAME_MATCH_RATIO`), applied in
  `env_override()` before the dataclass is built. Some keys, including the slskd and lidarr API settings
  are only processed in the global context (not per-source list). Env vars win over `config.yml`, including
  per-source overrides — keep this precedence when adding new config fields. List overrides are
  comma-separated, trimmed, and empty entries are removed; invalid booleans, numbers, and
  `LIDARR_SOURCES` values raise configuration errors. The loop interval uses `LOOP_INTERVAL`
  canonically, with `SCRIPT_INTERVAL` retained as fallback.
- Access resolved settings via `cfg.lidarr.*` / `cfg.slskd.*` (or `album.cfg.lidarr.*`/`album.cfg.slskd.*`),
  never raw dict lookups outside `from_yaml`.
- `config.yml` is read from disk once, at the top of `main()`, before the `--interval`/`LOOP_INTERVAL`
  loop starts. Config/interval changes require a process restart to take effect — this is
  intentional; do not reintroduce per-iteration reloading.
- `lidarr.import_timeout` bounds waiting for Lidarr import commands. Albums whose import command
  times out are held in the in-memory `pending_imports` set and skipped for the rest of the process,
  because the Lidarr command may still complete.
- Global module state (`lidarr`, `slskd`, `source_configs`, `folder_cache`, `broken_user`,
  `grabbed_albums`, `failed_import_denylist`, `remaining_albums_by_source`) is initialized once
  before the loop in `main()`, not reset per iteration. `grabbed_albums` persists across
  `--interval`/`LOOP_INTERVAL` loop iterations within the same process so albums grabbed while
  `disable_sync` is on aren't regrabbed on the next iteration (Lidarr never learns about them, so it
  can't tell us itself). `remaining_albums_by_source` caches unprocessed wanted albums, but each
  selected batch is rechecked against the current Lidarr queue. Failed-import filtering consults
  the live `failed_import_denylist` by album ID, so cached albums see later failures. Per-album search
  results live on `WantedAlbum.search_results` rather than a global cache, since they're only ever
  read by the same instance that populated them.
- `GrabbedAlbum` doesn't inherit `Album` or duplicate identity fields (`id`, `artist`, `title`,
  etc.) — it holds a `wanted_album: WantedAlbum` reference and exposes those as properties that
  delegate to it. Keep new grab-tracking state that's really about the underlying album (not the
  download attempt itself) on `WantedAlbum`, not duplicated onto `GrabbedAlbum`.
- Matching/enqueueing/import functions that take an `album` parameter (e.g. the old
  `album_match`, `check_ratio`, `try_enqueue`, `try_multi_enqueue`, `check_for_match`,
  `download_filter`, `release_format_accepted` on `WantedAlbum`; `trigger_lidarr_import`,
  `move_failed_import`, `refresh_download_status`, `downloads_all_done` on `GrabbedAlbum`) are
  methods on the relevant album class instead of free functions — add new album-scoped logic as a
  method on `WantedAlbum`/`GrabbedAlbum` rather than a module-level function taking an album
  argument. `album_match()` assigns Soulseek files one-to-one and returns only the matched files
  for enqueueing. Accepted release countries and formats are hard requirements; monitored releases
  may explicitly override them, while `allow_multi_disc=False` must block multi-disc enqueueing.
  `_cancel_and_delete_files(cfg, files)` is the one shared private helper (both classes'
  `cancel_and_delete` delegate to it, since `WantedAlbum` cancels an ad-hoc file list while
  `GrabbedAlbum` always cancels `self.files`).
- `safe_path()` is the shared validator for remote path components. It rejects unsafe components,
  resolves paths under the configured download root, and is used for moves and cleanup. Staging
  directories are created exclusively and destination collisions are rejected before moving.
- If `config.yml` is missing but a legacy Soularr `config.ini` is present, `migrate_soularr_ini_config`
  auto-generates a `config.yml` from it on the first run (one-time, best-effort field mapping) —
  keep the field mapping in sync if you rename/add config keys.

## Conventions

- [README.md](README.md) internal links use relative repo paths (e.g. `[config.yml](config.yml)`), not raw GitHub
  URLs — keep this pattern when adding links.

## Build, Run, Test

- No test suite exists in this repo.
- Run directly: `python soulseekarr.py --config-dir <dir> --var-dir <dir>` (defaults to CWD, or `/data`
  when `IN_DOCKER` is set). Use `--interval N` or `LOOP_INTERVAL` env var to loop instead of
  running once.
- Docker image builds from [Dockerfile](Dockerfile) (`python:3.14` base). The
  [docker workflow](.github/workflows/docker.yaml) only triggers when `soulseekarr.py` changes
  (`paths: soulseekarr.py`) — update that filter if you add other source files.
- Pyright is configured via [pyrightconfig.json](pyrightconfig.json) with `reportCallIssue` and
  `reportArgumentType` disabled (pyarr/slskd-api have incomplete type stubs).
