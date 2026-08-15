# Soulseekarr Agent Guidelines

## Architecture

Soulseekarr is a **single-file Python script** ([soulseekarr.py](soulseekarr.py)) — everything (config
dataclasses, Lidarr/Slskd API calls, matching logic, download monitoring, CLI entrypoint) lives
in this one module. Do not split it into a package unless the user explicitly asks for it.

- `LidarrConfig` / `SlskdConfig` / `AppConfig` (dataclasses) model `config.yml`. `AppConfig.from_yaml`
  merges the top-level `lidarr`/`slskd` defaults with an optional per-`source` override block
  (top-level `missing`/`cutoff_unmet` keys); `main()` builds one `source_configs[source]` entry per
  configured source, and each `Album`'s `cfg` field is bound to that source's `AppConfig` once, in
  `__post_init__`, at construction time.
- Every `lidarr.*`/`slskd.*` config value can also be overridden by an env var named
  `<SECTION>_<KEY>` (e.g. `LIDARR_API_KEY`, `SLSKD_MINIMUM_FILENAME_MATCH_RATIO`), applied in
  `env_override()` before the dataclass is built. Env vars win over `config.yml`, including
  per-source overrides — keep this precedence when adding new config fields.
- Access resolved settings via `cfg.lidarr.*` / `cfg.slskd.*` (or `album.cfg.lidarr.*`/`album.cfg.slskd.*`),
  never raw dict lookups outside `from_yaml`.
- `config.yml` is read from disk once, at the top of `main()`, before the `--interval`/`SCRIPT_INTERVAL`
  loop starts. Config/interval changes require a process restart to take effect — this is
  intentional; do not reintroduce per-iteration reloading.
- Global module state (`cfg`, `lidarr`, `slskd`, `source_configs`, `search_cache`, `folder_cache`,
  `broken_user`, `failed_import_denylist`, `remaining_albums_by_source`) is initialized once before
  the loop in `main()`, not reset per iteration. `grabbed_albums` persists across
  `--interval`/`SCRIPT_INTERVAL` loop iterations within the same process so albums grabbed while
  `disable_sync` is on aren't regrabbed on the next iteration (Lidarr never learns about them, so it
  can't tell us itself).
- If `config.yml` is missing but a legacy Soularr `config.ini` is present, `migrate_soularr_ini_config`
  auto-generates a `config.yml` from it on the first run (one-time, best-effort field mapping) —
  keep the field mapping in sync if you rename/add config keys.

## Conventions

- [README.md](README.md) internal links use relative repo paths (e.g. `[config.yml](config.yml)`), not raw GitHub
  URLs — keep this pattern when adding links.

## Build, Run, Test

- No test suite exists in this repo.
- Run directly: `python soulseekarr.py --config-dir <dir> --var-dir <dir>` (defaults to CWD, or `/data`
  when `IN_DOCKER` is set). Use `--interval N` or `SCRIPT_INTERVAL` env var to loop instead of
  running once.
- Docker image builds from [Dockerfile](Dockerfile) (`python:3.14` base). The
  [docker workflow](.github/workflows/docker.yaml) only triggers when `soulseekarr.py` changes
  (`paths: soulseekarr.py`) — update that filter if you add other source files.
- Pyright is configured via [pyrightconfig.json](pyrightconfig.json) with `reportCallIssue` and
  `reportArgumentType` disabled (pyarr/slskd-api have incomplete type stubs).
