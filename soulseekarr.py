#!/usr/bin/env python
from __future__ import annotations
import argparse
from typing import Literal, Iterator, Iterable, Self
import random
import re
import os
import sys
import time
import shutil
import difflib
import logging
import fcntl
from urllib.parse import urljoin
import copy
from dataclasses import dataclass, field
import music_tag
import slskd_api
import yaml
from collections import Counter
from requests.exceptions import HTTPError, RequestException
from pyarr import LidarrAPI
from pyarr.exceptions import PyarrError
from migration import DEFAULT_LOGGING_CONF, expand_env_vars, migrate_soularr_ini_config

logger = logging.getLogger("soulseekarr")

TRANSFER_APPEAR_TIMEOUT = 30

def env_override(section: str, key: str, value):
    """Override a resolved config value with an env var named "<SECTION>_<KEY>".

    Matches the YAML path (e.g. lidarr.api_key -> LIDARR_API_KEY), coercing the env var to
    match the existing value's type.
    """
    env_name = f"{section}_{key}".upper()
    raw = os.environ.get(env_name)
    if raw is None:
        return value
    if isinstance(value, bool):
        normalized = raw.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{env_name} must be a boolean, got {raw!r}")
    if isinstance(value, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, int):
        try:
            return int(raw)
        except ValueError as ex:
            raise ValueError(f"{env_name} must be an integer, got {raw!r}") from ex
    if isinstance(value, float):
        try:
            return float(raw)
        except ValueError as ex:
            raise ValueError(f"{env_name} must be a number, got {raw!r}") from ex
    return raw


AlbumSource = Literal["missing", "cutoff_unmet"]


@dataclass
class AlbumState:
    """State shared by every instance representing the same Lidarr album."""
    grabbed: bool = False
    import_failed: bool = False
    import_pending: bool = False


@dataclass
class LidarrConfig:
    api_key: str
    host_url: str
    url_base: str
    download_dir: str
    import_timeout: int
    disable_sync: bool
    sources: list[AlbumSource]
    search_type: str
    chunk_size: int
    sort_key: str
    sort_dir: str
    title_blacklist: list[str]
    failed_import_denylist: bool
    use_selected_lidarr_release: bool
    use_most_common_tracknum: bool
    allow_multi_disc: bool
    accepted_countries: list[str]
    skip_region_check: bool
    accepted_formats: list[str]


@dataclass
class SlskdConfig:
    api_key: str
    host_url: str
    download_dir: str
    failed_imports_dir: str
    url_base: str
    stalled_timeout: int
    remote_queue_timeout: int
    delete_searches: bool
    remove_completed_downloads: bool
    requeue_failed_downloads: bool
    timeout: int
    maximum_peer_queue: int
    minimum_peer_upload_speed: int
    minimum_match_ratio: float
    minimum_search_interval: int
    ignored_users: list[str]
    search_blacklist: list[str]
    album_prepend_artist: bool
    filtering: bool
    extensions_whitelist: list[str]
    rename_download_folders: bool
    allowed_filetypes: list[str]


@dataclass
class AppConfig:
    source: AlbumSource | None
    lidarr: LidarrConfig
    slskd: SlskdConfig
    interval: int

    @classmethod
    def from_yaml(cls, data: dict, args, source: AlbumSource | None = None) -> "AppConfig":
        """Build an AppConfig from parsed config.yml data, env var overrides, and CLI args.

        Layers this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the
        top-level `lidarr`/`slskd` defaults, then applies per-key env var overrides.
        """
        lidarr_cfg: dict = data.get("lidarr") or {}
        slskd_cfg: dict = data.get("slskd") or {}

        # Layer this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the top-level lidarr/slskd defaults.
        source_cfg: dict = (data.get(source) or {}) if source else {}
        resolved_lidarr = {**lidarr_cfg, **(source_cfg.get("lidarr") or {})}
        resolved_slskd = {**slskd_cfg, **(source_cfg.get("slskd") or {})}
        sources = list(env_override("lidarr", "sources", resolved_lidarr.get("sources", ["missing"])))
        invalid_sources = [source for source in sources if source not in ("missing", "cutoff_unmet")]
        if invalid_sources:
            raise ValueError(f"LIDARR_SOURCES contains unsupported values: {invalid_sources}")

        lidarr = LidarrConfig(
            api_key=env_override("lidarr", "api_key", resolved_lidarr.get("api_key")),
            host_url=env_override("lidarr", "host_url", resolved_lidarr.get("host_url")),
            url_base=str(env_override("lidarr", "url_base", resolved_lidarr.get("url_base", "/"))),
            download_dir=env_override("lidarr", "download_dir", resolved_lidarr.get("download_dir")),
            import_timeout=int(env_override("lidarr", "import_timeout", resolved_lidarr.get("import_timeout", 3600))),
            disable_sync=bool(env_override("lidarr", "disable_sync", resolved_lidarr.get("disable_sync", False))),
            sources=sources,
            search_type=str(env_override("lidarr", "search_type", resolved_lidarr.get("search_type", "incrementing"))).lower().strip(),
            chunk_size=int(env_override("lidarr", "chunk_size", resolved_lidarr.get("chunk_size", 10))),
            sort_key=str(env_override("lidarr", "sort_key", resolved_lidarr.get("sort_key", "albums.title"))).strip(),
            sort_dir=str(env_override("lidarr", "sort_dir", resolved_lidarr.get("sort_dir", "ascending"))).strip().lower(),
            title_blacklist=env_override("lidarr", "title_blacklist", resolved_lidarr.get("title_blacklist", [])),
            failed_import_denylist=bool(env_override("lidarr", "failed_import_denylist", resolved_lidarr.get("failed_import_denylist", True))),
            use_selected_lidarr_release=bool(env_override("lidarr", "use_selected_lidarr_release", resolved_lidarr.get("use_selected_lidarr_release", False))),
            use_most_common_tracknum=bool(env_override("lidarr", "use_most_common_tracknum", resolved_lidarr.get("use_most_common_tracknum", True))),
            allow_multi_disc=bool(env_override("lidarr", "allow_multi_disc", resolved_lidarr.get("allow_multi_disc", True))),
            accepted_countries=env_override(
                "lidarr",
                "accepted_countries",
                resolved_lidarr.get("accepted_countries", ["Europe", "Japan", "United Kingdom", "United States", "[Worldwide]", "Australia", "Canada"]),
            ),
            skip_region_check=bool(env_override("lidarr", "skip_region_check", resolved_lidarr.get("skip_region_check", False))),
            accepted_formats=env_override("lidarr", "accepted_formats", resolved_lidarr.get("accepted_formats", ["CD", "Digital Media", "Vinyl"])),
        )

        # Pre-set this so we can derive the fallback failed_imports_dir in the download dir
        slskd_download_dir = env_override("slskd", "download_dir", resolved_slskd.get("download_dir"))

        slskd = SlskdConfig(
            api_key=env_override("slskd", "api_key", resolved_slskd.get("api_key")),
            host_url=env_override("slskd", "host_url", resolved_slskd.get("host_url")),
            download_dir=slskd_download_dir,
            failed_imports_dir=str(
                env_override("slskd", "failed_imports_dir", resolved_slskd.get("failed_imports_dir") or os.path.join(slskd_download_dir, "failed_imports"))
            ),
            url_base=str(env_override("slskd", "url_base", resolved_slskd.get("url_base", "/"))),
            stalled_timeout=int(env_override("slskd", "stalled_timeout", resolved_slskd.get("stalled_timeout", 3600))),
            remote_queue_timeout=int(env_override("slskd", "remote_queue_timeout", resolved_slskd.get("remote_queue_timeout", 300))),
            delete_searches=bool(env_override("slskd", "delete_searches", resolved_slskd.get("delete_searches", True))),
            remove_completed_downloads=bool(env_override("slskd", "remove_completed_downloads", resolved_slskd.get("remove_completed_downloads", True))),
            requeue_failed_downloads=bool(env_override("slskd", "requeue_failed_downloads", resolved_slskd.get("requeue_failed_downloads", True))),
            timeout=int(env_override("slskd", "timeout", resolved_slskd.get("timeout", 10))),
            maximum_peer_queue=int(env_override("slskd", "maximum_peer_queue", resolved_slskd.get("maximum_peer_queue", 50))),
            minimum_peer_upload_speed=int(env_override("slskd", "minimum_peer_upload_speed", resolved_slskd.get("minimum_peer_upload_speed", 0))),
            minimum_match_ratio=float(env_override("slskd", "minimum_filename_match_ratio", resolved_slskd.get("minimum_filename_match_ratio", 0.5))),
            minimum_search_interval=int(env_override("slskd", "minimum_search_interval", resolved_slskd.get("minimum_search_interval", 5))),
            ignored_users=env_override("slskd", "ignored_users", resolved_slskd.get("ignored_users", [])),
            search_blacklist=env_override("slskd", "search_blacklist", resolved_slskd.get("search_blacklist", [])),
            album_prepend_artist=bool(env_override("slskd", "album_prepend_artist", resolved_slskd.get("album_prepend_artist", False))),
            filtering=bool(env_override("slskd", "filtering", resolved_slskd.get("filtering", False))),
            extensions_whitelist=env_override("slskd", "extensions_whitelist", resolved_slskd.get("extensions_whitelist", ["txt", "nfo", "jpg"])),
            rename_download_folders=bool(env_override("slskd", "rename_download_folders", resolved_slskd.get("rename_download_folders", True))),
            allowed_filetypes=env_override("slskd", "allowed_filetypes", resolved_slskd.get("allowed_filetypes", ["flac", "mp3"])),
        )

        return cls(
            source=source,
            lidarr=lidarr,
            slskd=slskd,
            interval=int(
                args.interval
                if args.interval is not None
                else os.getenv(
                    "LOOP_INTERVAL",
                    os.getenv("SCRIPT_INTERVAL", data.get("interval", 300 if is_docker() else 0)),
                )
            ),
        )


@dataclass
class Album:
    """Identity, configuration, and persistent state for a Lidarr album."""
    id: int
    artistId: int
    artist: str
    title: str
    year: str
    source: AlbumSource
    cfg: AppConfig = field(init=False)
    state: AlbumState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            self.cfg = source_configs[self.source]
        except KeyError:
            raise ValueError(
                f"Unknown source {self.source!r} "
                f"for album {self.artist} - {self.title}"
            ) from None

            self.state = album_states.setdefault(self.id, AlbumState())

@dataclass
class WantedAlbum(Album):
    """A pruned Lidarr wanted album and its current search state.

    Lidarr's raw wanted-list response nests far more per album (images, ratings, full release
    media/tracks, statistics, etc.); trimming to this shape keeps memory use sane for large
    libraries. See prune_wanted_record().
    """
    failure: str | None = field(default=None, init=False)
    search_results: dict = field(default_factory=dict, init=False, repr=False)
    folder_cache: dict = field(default_factory=dict, init=False, repr=False)

    def skip_reason(self) -> str | None:
        """Return why this album should be skipped, or None if it is eligible."""
        if self.cfg.lidarr.failed_import_denylist and self.state.import_failed:
            return "failed import"
        if self.state.import_pending:
            return "pending Lidarr import"
        if self.cfg.lidarr.disable_sync and self.state.grabbed:
            return "already grabbed"

        for word in self.cfg.lidarr.title_blacklist:
            if word and word.lower() in self.title.lower():
                return f"blacklisted word {word!r}"

        return None

    def queue_download(self) -> GrabbedAlbum | None:
        """Search for this album and enqueue the best available match."""
        self.failure = None

        if not self.search_for_album():
            self.failure = "Search failed"
            return None

        download = self.find_download()
        if download is None:
            self.failure = "No matching download"

        return download

    def search_for_album(self) -> bool:
        """Search slskd for an album, poll until the search completes, and cache matching results.

        Builds the search query (optionally prepending the artist name and stripping blacklisted
        words), waits for the slskd search to finish or time out, and populates `search_results`
        with each result's files grouped by user and allowed filetype.
        """

        if len(self.title) == 1:  # Need to add some code to wrangle specific artist names in here.. ;)
            query = self.artist + " " + self.title
        else:
            query = self.artist + " " + self.title if self.cfg.slskd.album_prepend_artist else self.title

        original_query = query
        for word in self.cfg.slskd.search_blacklist:
            if word:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(word), re.IGNORECASE)
                query = pattern.sub("", query)

        # Clean up double spaces
        query = " ".join(query.split())

        if query != original_query:
            logger.info(f"Filtered search query: '{original_query}' -> '{query}'")
        if not query:
            logger.warning(f"Skipping search for {self.artist} - {self.title}: query is empty after filtering")
            return False

        logger.info(f"Searching for '{self.source}' album: {self.artist} - {self.title} (query: '{query}')")
        try:
            search = slskd.searches.search_text(
                searchText=query,
                searchTimeout=max(1, int(self.cfg.slskd.timeout * 1000)),
                filterResponses=True,
                maximumPeerQueueLength=self.cfg.slskd.maximum_peer_queue,
                minimumPeerUploadSpeed=self.cfg.slskd.minimum_peer_upload_speed,
            )
        except Exception:
            logger.exception(f"Failed to perform search via SLSKD: {query}")
            return False

        def cleanup_search():
            """Best-effort delete so a timeout/error doesn't leave the search stuck in SLSKD forever."""
            if self.cfg.slskd.delete_searches:
                try:
                    slskd.searches.delete(search["id"])
                except Exception:
                    logger.warning(f"Failed to delete search {search['id']} from SLSKD", exc_info=True)

        # Add timeout here to increase reliability with Slskd. Sometimes it doesn't update search status fast enough. More of an issue with lots of historical searches in slskd
        time.sleep(5)
        start_time = time.time()
        try:
            while True:
                if slskd.searches.state(search["id"], False)["state"] != "InProgress":  # Added False here as we don't want the search results here. Just the state.
                    break
                if (time.time() - start_time) > self.cfg.slskd.timeout:
                    logger.error("Failed to perform search via SLSKD due to timeout on search results.")
                    return False
                time.sleep(1)

            search_results = slskd.searches.search_responses(search["id"])
            logger.info(f"Search returned {len(search_results)} results")
        except Exception:
            logger.exception(f"Failed to perform search via SLSKD: {query}")
            return False
        finally:
            cleanup_search()

        if not search_results:
            return False

        for result in search_results:
            username = result["username"]
            user_results = self.search_results.setdefault(username, {})

            logger.info(f"Caching and truncating results for user: {username}")
            for file in result["files"]:
                file_dir = file["filename"].rsplit("\\", 1)[0]
                for allowed_filetype in self.cfg.slskd.allowed_filetypes:
                    if verify_filetype(file, allowed_filetype):
                        file_dirs = user_results.setdefault(allowed_filetype, [])
                        if file_dir not in file_dirs:
                            file_dirs.append(file_dir)
        return True

    def find_download(self) -> GrabbedAlbum | None:
        """Find and enqueue a matching release in one of the allowed filetypes."""
        results = self.search_results
        releases = lidarr.get_album(self.id)["releases"]
        tracks_by_release = {}

        for filetype in self.cfg.slskd.allowed_filetypes:
            if not any(filetype in result for result in results.values()):
                logger.debug(f"No search results for Quality: {filetype}. Skipping.")
                continue

            remaining = list(releases)
            while remaining:
                release = self.choose_release(remaining)
                if release is None:
                    break
                remaining.remove(release)

                release_id = release["id"]
                if release_id not in tracks_by_release:
                    tracks_by_release[release_id] = lidarr.get_tracks(
                        artistId=self.artistId,
                        albumId=self.id,
                        albumReleaseId=release_id,
                    )
                tracks = tracks_by_release[release_id]

                downloads = self.try_enqueue(tracks, results, filetype)
                if (
                    not downloads
                    and self.cfg.lidarr.allow_multi_disc
                    and len(release["media"]) > 1
                ):
                    downloads = self.try_multi_enqueue(
                        release, tracks, results, filetype
                    )
                if downloads:
                    return GrabbedAlbum(wanted_album=self, files=downloads)

        return None

    def try_enqueue(self, tracks, results, filetype):
        """Find and enqueue a single-directory album match."""
        for username, user_results in results.items():
            file_dirs = user_results.get(filetype)
            if not file_dirs:
                continue

            logger.debug(f"Parsing result from user: {username}")
            match = self.check_for_match(tracks, filetype, file_dirs, username)
            if match:
                downloads = self._enqueue_match(username, filetype, *match)
                if downloads:
                    return downloads

        logger.info(f"Failed to enqueue {self.artist} - {self.title}")
        return None

    def try_multi_enqueue(self, release, tracks, results, filetype):
        """Find every disc of a multi-disc release, then enqueue them together."""
        media = release["media"]
        if not self.cfg.lidarr.allow_multi_disc or len(media) <= 1:
            return None

        sources = []
        used_sources = set()

        for medium in media:
            disk_no = medium["mediumNumber"]
            disk_tracks = [
                track for track in tracks
                if track["mediumNumber"] == disk_no
            ]

            for username, user_results in results.items():
                file_dirs = [
                    file_dir for file_dir in user_results.get(filetype, [])
                    if (username, file_dir) not in used_sources
                ]
                match = self.check_for_match(
                    disk_tracks, filetype, file_dirs, username
                )
                if match:
                    sources.append((disk_no, username, *match))
                    used_sources.add((username, match[1]))
                    break
            else:
                return None

        all_downloads = []
        for disk_no, username, directory, file_dir, required_files in sources:
            downloads = self._enqueue_match(
                username, filetype, directory, file_dir, required_files
            )
            if not downloads:
                if all_downloads:
                    self.cancel_and_delete(all_downloads)
                return None

            for file in downloads:
                file["disk_no"] = disk_no
                file["disk_count"] = len(media)
            all_downloads.extend(downloads)

        return all_downloads

    def _enqueue_match(
        self,
        username,
        filetype,
        directory,
        file_dir,
        required_files,
    ):
        """Prepare and enqueue one matched directory."""
        directory = self.download_filter(filetype, directory)
        files = {file["filename"]: file for file in directory["files"]}
        files.update({file["filename"]: file for file in required_files})
        required_names = {file["filename"] for file in required_files}

        for filename, file in files.items():
            file["required"] = filename in required_names
            file["filename"] = f"{file_dir}\\{filename}"

        try:
            downloads = slskd_do_enqueue(
                username, list(files.values()), file_dir
            )
        except Exception:
            logger.exception(
                f"Exception enqueueing {self.artist} - "
                f"{self.title} from {username}"
            )
            return None

        accepted = {file["filename"] for file in downloads or []}
        required = {
            file["filename"] for file in files.values()
            if file["required"]
        }

        if downloads and required <= accepted:
            return downloads
        if downloads:
            self.cancel_and_delete(downloads)

        logger.info(
            f"Failed to enqueue {self.artist} - {self.title} from {username}"
        )
        return None

    def check_for_match(self, tracks, filetype, file_dirs, username):
        """Return the first directory containing a match for every required track."""
        user_cache = self.folder_cache.setdefault(username, {})

        for file_dir in file_dirs:
            if file_dir in user_cache:
                logger.debug(f"Using cached directory for {username}: {file_dir}")
                directory = copy.deepcopy(user_cache[file_dir])
            else:
                logger.debug(f"Fetching directory for {username}: {file_dir}")
                try:
                    directory = slskd.users.directory(
                        username=username, directory=file_dir
                    )[0]
                except HTTPError as ex:
                    status = (
                        ex.response.status_code
                        if ex.response is not None
                        else "unknown"
                    )
                    logger.warning(
                        f'HTTP error getting directory from "{username}" '
                        f'folder "{file_dir}": {status}'
                    )
                    if ex.response is not None and ex.response.text:
                        logger.debug(
                            f"SLSKD response body: {ex.response.text[:500]}"
                        )
                    continue
                except IndexError:
                    logger.warning(
                        f'Empty directory response from "{username}" '
                        f'for "{file_dir}"'
                    )
                    directory = {"files": []}
                except RequestException:
                    logger.exception(
                        f'Network error getting directory from user: "{username}"'
                    )
                    return None
                except Exception:
                    logger.exception(
                        f'Error getting directory from user: "{username}"'
                    )
                    return None

                user_cache[file_dir] = copy.deepcopy(directory)

            audio_files = [
                file for file in directory["files"]
                if verify_filetype(file, filetype)
            ]

            # Allow directories containing additional bonus tracks.
            if len(audio_files) < len(tracks):
                continue

            matched_files = self.album_match(
                tracks, audio_files, username, filetype
            )
            if matched_files:
                return directory, file_dir, matched_files

        return None

    def download_filter(self, filetype, directory):
        """Limit a directory to the configured audio type and optional sidecars."""
        if self.cfg.slskd.filtering:
            extensions = {
                ext.lower()
                for ext in self.cfg.slskd.extensions_whitelist
            }
            extensions.add(filetype.split()[0].lower())
            directory["files"] = [
                file for file in directory["files"]
                if file["filename"].rsplit(".", 1)[-1].lower() in extensions
            ]
            logger.debug(f"Accepted extensions: {sorted(extensions)}")

        return directory

    def album_match(self, lidarr_tracks, slskd_tracks, username, filetype):
        """Return a one-to-one file match for every Lidarr track, or None.

        Compares each Lidarr track title against every candidate filename with fuzzy string
        matching (plus several filename-cleanup heuristics), requiring every track to clear
        `slskd.minimum_match_ratio` for the album to count as matched. Each candidate file can
        only be assigned to one track.
        """

        available_files = list(slskd_tracks)
        matched_files = []
        total_match = 0.0
        filetype_ext = filetype.split(" ")[0]

        for lidarr_track in lidarr_tracks:
            lidarr_filename = lidarr_track["title"] + "." + filetype_ext
            best_match = 0.0
            best_file = None

            for slskd_track in available_files:
                slskd_filename = slskd_track["filename"]

                # Try to match the ratio with the exact filenames
                ratio = difflib.SequenceMatcher(None, lidarr_filename, slskd_filename).ratio()

                # If ratio is a bad match try and split off (with " " as the separator) the garbage
                # at the start of the slskd_filename and try again
                ratio = self.check_ratio(" ", ratio, lidarr_filename, slskd_filename)
                # Same but with "_" as the separator
                ratio = self.check_ratio("_", ratio, lidarr_filename, slskd_filename)

                # Same checks but prepend album name.
                ratio = self.check_ratio("", ratio, self.title + " " + lidarr_filename, slskd_filename)
                ratio = self.check_ratio(" ", ratio, self.title + " " + lidarr_filename, slskd_filename)
                ratio = self.check_ratio("_", ratio, self.title + " " + lidarr_filename, slskd_filename)

                if ratio > best_match:
                    best_match = ratio
                    best_file = slskd_track
                    if best_match == 1.0:  # Can't do better than a perfect match
                        break

            if best_file is None or best_match < self.cfg.slskd.minimum_match_ratio:
                return None
            available_files.remove(best_file)
            matched_files.append(best_file)
            total_match += best_match

        if matched_files and username not in self.cfg.slskd.ignored_users:
            logger.info(f"Found match from user: {username} for {len(matched_files)} tracks! Track attributes: {filetype}")
            logger.info(f"Average sequence match ratio: {total_match / len(matched_files)}")
            logger.info("SUCCESSFUL MATCH")
            logger.info("-------------------")
            return matched_files

        return None

    def check_ratio(self, separator, ratio, lidarr_filename, slskd_filename):
        if ratio < self.cfg.slskd.minimum_match_ratio:
            if separator != "":
                lidarr_filename_word_count = len(lidarr_filename.split()) * -1
                truncated_slskd_filename = " ".join(slskd_filename.split(separator)[lidarr_filename_word_count:])
                ratio = difflib.SequenceMatcher(None, lidarr_filename, truncated_slskd_filename).ratio()
            else:
                ratio = difflib.SequenceMatcher(None, lidarr_filename, slskd_filename).ratio()

            return ratio
        return ratio

    def cancel_and_delete(self, files) -> None:
        """Cancel each in-progress slskd download in `files` and remove its local download folder."""
        _cancel_and_delete_files(self.cfg, files)

    def add_to_failed_import_denylist(self) -> None:
        """Remember this failed import for the lifetime of the process."""
        if self.state.import_failed:
            return
        self.state.import_failed = True
        logger.info(
            f"Added to failed import denylist: "
            f"{self.artist} - {self.title} (ID: {self.id})"
        )

    def choose_release(self, releases):
        """Pick the best release to search for from an album's list of Lidarr releases.

        Prefers the release manually selected in Lidarr (if `use_selected_lidarr_release`), then
        the first release matching the accepted countries/formats/status/track-count settings.
        Accepted countries and formats are requirements, not fallback preferences.
        """

        def is_multi_disc(release) -> bool:
            return len(release.get("media", [])) > 1 or release.get("mediumCount", 1) > 1

        if self.cfg.lidarr.use_selected_lidarr_release:
            for release in releases:
                if not self.cfg.lidarr.allow_multi_disc and is_multi_disc(release):
                    continue
                if release.get("monitored"):
                    logger.info(f"Using selected Lidarr release for {self.artist}: {release['format']}, {release['trackCount']} tracks, ID: {release['id']}")
                    return release

        def is_eligible(release) -> bool:
            country = release["country"][0] if release["country"] else None
            return (
                (self.cfg.lidarr.allow_multi_disc or not is_multi_disc(release))
                and
                (self.cfg.lidarr.skip_region_check or country in self.cfg.lidarr.accepted_countries)
                and self.release_format_accepted(release)
                and release["status"] == "Official"
            )

        most_common_trackcount = release_trackcount_mode([release for release in releases if is_eligible(release)])

        for release in releases:
            if not self.cfg.lidarr.allow_multi_disc and is_multi_disc(release):
                continue
            country = release["country"][0] if release["country"] else None
            track_count_ok = not self.cfg.lidarr.use_most_common_tracknum or release["trackCount"] == most_common_trackcount

            if is_eligible(release) and track_count_ok:
                logger.info(
                    ", ".join(
                        [
                            f"Selected release for {self.artist}: {release['status']}",
                            str(country),
                            release['format'],
                            f"Mediums: {release['mediumCount']}",
                            f"Tracks: {release['trackCount']}",
                            f"ID: {release['id']}",
                        ]
                    )
                )

                return release

        return None

    def release_format_accepted(self, release) -> bool:
        """Check a release's format against `accepted_formats`, unwrapping multi-disc prefixes (e.g. "2xCD" -> "CD")."""
        fmt = release["format"]
        match = re.match(r"^\d+x", fmt, re.IGNORECASE)
        if match:
            if not self.cfg.lidarr.allow_multi_disc:
                return False
            fmt = fmt[match.end():]
        return fmt in self.cfg.lidarr.accepted_formats


@dataclass
class WantedAlbums:
    """A list of Lidarr wanted albums, with a few convenience methods for filtering and sorting."""
    albums: list[WantedAlbum] = field(default_factory=list)

    def __iter__(self) -> Iterator[WantedAlbum]:
        return iter(self.albums)

    def __len__(self) -> int:
        return len(self.albums)

    def append(self, album: WantedAlbum) -> None:
        self.albums.append(album)

    def extend(self, albums: Iterable[WantedAlbum]) -> None:
        self.albums.extend(albums)

    def take(self, count: int) -> Self:
        """Remove and return the first `count` albums."""
        selected = self.albums[:count]
        del self.albums[:count]
        return type(self)(albums=selected)

    def shuffle(self) -> None:
        random.shuffle(self.albums)

    def deduplicate(self) -> Self:
        """Keep one album per ID, preferring the stricter cutoff-unmet source."""
        unique_albums: dict[int, WantedAlbum] = {}
        for album in self.albums:
            current = unique_albums.get(album.id)
            if current is None or album.source == "cutoff_unmet":
                unique_albums[album.id] = album
        return type(self)(albums=list(unique_albums.values()))

    def filter_list(self) -> Self:
        """Return albums that are eligible to be searched."""
        filtered = []

        for album in self.albums:
            reason = album.skip_reason()
            if reason:
                logger.info(
                    f"Skipping {reason}: {album.artist} - "
                    f"{album.title} (ID: {album.id})"
                )
            else:
                filtered.append(album)

        return type(self)(albums=filtered)

    def grab_most_wanted(self) -> int:
        """Search, download, and import every album in this collection."""
        downloads = self.search_and_queue()

        logger.info(f"Total downloads added: {len(downloads)}")
        for album in downloads:
            logger.info(f"Album: {album.title} Artist: {album.artist}")
        downloads.monitor_downloads()

        failures = [album for album in self.albums if album.failure]
        logger.info(f"Failed albums: {len(failures)}")

        for album in failures:
            logger.info(f"{album.failure}: {album.artist} - {album.title}")

        return len(failures)

    def search_and_queue(self) -> GrabbedAlbums:
        """Search and enqueue every album, respecting the search interval."""
        downloads = GrabbedAlbums()
        last_index = len(self.albums) - 1

        for index, album in enumerate(self.albums):
            search_start = time.time()
            download = album.queue_download()

            if download:
                downloads.append(download)

            remaining = (
                album.cfg.slskd.minimum_search_interval
                - (time.time() - search_start)
            )
            if index < last_index and remaining > 0:
                logger.info(
                    f"Waiting {remaining:.1f}s to meet "
                    "minimum_search_interval"
                )
                time.sleep(remaining)

        return downloads

@dataclass
class GrabbedAlbum:
    """An album's enqueued downloads, tracked from enqueue through Lidarr import.

    Built once by find_download(), then mutated in place by monitor_downloads() and
    process_completed_album()/trigger_lidarr_import() as the download/import progresses.
    Identity fields (id, artist, title, ...) are read from `wanted_album` instead of being
    duplicated here.
    """
    MAX_FILE_RETRIES = 4
    SUCCESS = "Completed, Succeeded"
    REMOTE = "Queued, Remotely"

    HARD_ERRORS = {
        "Completed, Cancelled",
        "Completed, TimedOut",
        "Completed, Errored",
        "Completed, Aborted",
    }
    ERRORS = HARD_ERRORS | {"Completed, Rejected"}
    STABLE = {SUCCESS, REMOTE, "Queued, Locally"}

    wanted_album: WantedAlbum
    files: list[dict]
    count_start: float = field(default_factory=time.time)
    rejected_retries: int = 0
    import_folder: str | None = None
    staging_folder: str | None = None

    @property
    def id(self) -> int:
        return self.wanted_album.id

    @property
    def artist(self) -> str:
        return self.wanted_album.artist

    @property
    def title(self) -> str:
        return self.wanted_album.title

    @property
    def year(self) -> str:
        return self.wanted_album.year

    @property
    def cfg(self) -> AppConfig:
        return self.wanted_album.cfg

    @property
    def required_files(self) -> list[dict]:
        return [
            file for file in self.files
            if file.get("required", True)
        ]

    @staticmethod
    def file_state(file: dict) -> str | None:
        return (file.get("status") or {}).get("state")

    def process_completed_album(self) -> None:
        """Move a fully-downloaded album into its import folder and trigger a Lidarr import.

        Renames/moves the downloaded files into a single folder, tags them, and asks Lidarr to
        scan it. Rolls back the moved files if anything fails partway through, and records a
        failure if the Lidarr import itself fails. If `disable_sync` is set, skips the Lidarr
        import entirely and just tracks the grab.
        """

        if self.cfg.slskd.rename_download_folders:
            staging_folder_name = sanitize_folder_name(self.artist + " - " + self.title + " (" + self.year + ")")
        else:
            staging_folder_name = self.files[0]["file_dir"].split("\\")[-1]
        self.staging_folder = safe_path(self.cfg.slskd.download_dir, staging_folder_name)
        if self.staging_folder is None:
            logger.error("Refusing to use an invalid slskd download directory")
            self.wanted_album.failure = "Download processing failed"
            return
        self.import_folder = os.path.join(self.cfg.lidarr.download_dir, staging_folder_name)

        source_dirs = {
            safe_path(self.cfg.slskd.download_dir, file["file_dir"].split("\\")[-1])
            for file in self.files
        }
        if None in source_dirs:
            logger.error("Refusing to use an invalid slskd source directory")
            self.wanted_album.failure = "Download processing failed"
            return
        staging_folder_created = False
        if os.path.exists(self.staging_folder):
            if not os.path.isdir(self.staging_folder) or self.staging_folder not in source_dirs:
                logger.error(f"Refusing to reuse existing staging directory: {self.staging_folder}")
                self.wanted_album.failure = "Download processing failed"
                return
        else:
            try:
                os.mkdir(self.staging_folder)
                staging_folder_created = True
            except OSError:
                logger.exception(f"Failed to create staging directory: {self.staging_folder}")
                self.wanted_album.failure = "Download processing failed"
                return

        success, rm_dirs = self.move_album_files()
        if not success:
            if staging_folder_created:
                try:
                    os.rmdir(self.staging_folder)
                except OSError:
                    logger.warning(f"Could not remove temp import directory {self.staging_folder}")
                    self.wanted_album.failure = "Download processing failed"
            return

        for rm_dir in rm_dirs:
            if rm_dir != self.staging_folder:
                try:
                    os.rmdir(rm_dir)
                except OSError:
                    logger.warning(f"Skipping removal of {rm_dir} because it's not empty.")

        if self.cfg.lidarr.disable_sync:
            logger.info(f"Sync disabled. Skipping Lidarr import of {self.artist} - {self.title}")
            self.wanted_album.state.grabbed = True
            return

        logger.info(f"Attempting Lidarr import of {self.artist} - {self.title}")
        self.tag_album_files()
        self.trigger_lidarr_import()

    def tag_album_files(self):
        """Tag each downloaded file with album/artist metadata (and disk info for multi-disc albums)."""
        for file in self.files:
            try:
                song = music_tag.load_file(file["import_path"])
            except NotImplementedError:
                continue  # Not a supported audio file (e.g. jpg, nfo)
            except Exception:
                logger.exception(f"Error loading file for tagging: {file['import_path']}")
                continue
            if song is None:
                continue
            try:
                if "disk_no" in file:
                    song["discnumber"] = file["disk_no"]
                    song["totaldiscs"] = file["disk_count"]
                song["albumartist"] = self.artist
                song["album"] = self.title
                song.save()
            except Exception:
                logger.exception(f"Error writing tags for: {file['import_path']}")

    def move_album_files(self):
        """Move/rename each file into the shared import folder, tracking source folders to clean up.

        Returns (success, rm_dirs). On failure, already-moved files are rolled back to their
        original location and the caller is responsible for removing the (now-empty) import folder.
        """
        files = self.files
        rm_dirs = []
        moved_files_history = []
        move_plan = []
        for file in files:
            file_folder = file["file_dir"].split("\\")[-1]
            filename = file["filename"].split("\\")[-1]
            src_file = safe_path(self.cfg.slskd.download_dir, file_folder, filename)
            if src_file is None:
                logger.error("Refusing to move a file from an invalid slskd download path")
                return False, rm_dirs
            src_folder = os.path.dirname(src_file)
            if src_folder not in rm_dirs:
                rm_dirs.append(src_folder)  # Multi disk albums are sometimes in multiple folders. eg. CD01 CD02. So we need to clean up both
            if "disk_no" in file and "disk_count" in file and file["disk_count"] > 1:
                filename = f"Disk {file['disk_no']} - {filename}"
            dst_file = safe_path(self.staging_folder, filename)
            if dst_file is None:
                logger.error("Refusing to move a file to an invalid staging path")
                return False, rm_dirs
            move_plan.append((file, src_file, dst_file))

        for file, src_file, dst_file in move_plan:
            file["import_path"] = dst_file
            if os.path.abspath(src_file) == os.path.abspath(dst_file):
                continue
            try:
                if os.path.lexists(dst_file):
                    raise FileExistsError(f"Destination already exists: {dst_file}")
                shutil.move(src_file, dst_file)
                moved_files_history.append((src_file, dst_file))
            except Exception:
                logger.exception(f"Failed to move: {file['filename']} to temp location for import into Lidarr. Rolling back...")
                for src, dst in reversed(moved_files_history):
                    try:
                        shutil.move(dst, src)
                    except Exception:
                        logger.exception(f"Critical failure during rollback: could not move {dst} back to {src}")
                return False, rm_dirs
        return True, rm_dirs

    def refresh_download_status(self) -> bool:
        """Fetch and attach the current slskd transfer status to each file in `files`."""
        ok = True
        for file in self.files:
            try:
                status = slskd.transfers.get_download(file["username"], file["id"])
                if not isinstance(status, dict) or not isinstance(status.get("state"), str) or not status["state"]:
                    raise ValueError("missing or malformed download status")
                file["status"] = status
            except Exception:
                logger.exception(f"Error getting download status of {file['filename']}")
                file["status"] = None
                if file.get("required", True):
                    ok = False
        return ok

    def download_progress(self) -> tuple[bool, list[dict], int]:
        """Return completion, problem files, and remote-queued count."""
        files = self.required_files
        states = [self.file_state(file) for file in files]
        problems = [
            file for file in files
            if self.file_state(file) in self.ERRORS
        ]

        return (
            all(state == self.SUCCESS for state in states),
            problems,
            states.count(self.REMOTE),
        )

    def discard_incomplete_optional_files(self) -> None:
        """Cancel optional transfers that did not complete before required files finished."""
        optional_files = [
            file for file in self.files
            if not file.get("required", True)
            and (file.get("status") or {}).get("state") != "Completed, Succeeded"
        ]
        if optional_files:
            _cancel_and_delete_files(self.cfg, optional_files)
            self.files = [file for file in self.files if file not in optional_files]

    def cancel_and_delete(self) -> None:
        """Cancel each in-progress slskd download for this album and remove its local download folder."""
        _cancel_and_delete_files(self.cfg, self.files)

    def fail(self, reason: str) -> bool:
        """Cancel this album, record its failure, and mark it terminal."""
        self.cancel_and_delete()
        self.wanted_album.failure = reason
        logger.info(f"{reason}: {self.artist} - {self.title}")
        return True

    def requeue_file(self, file: dict) -> bool:
        """Requeue one failed transfer and update its slskd ID."""
        logger.info(f"Download error. Requeue file: {file['filename']}")

        downloads = slskd_do_enqueue(
            file["username"],
            [{
                "filename": file["filename"],
                "size": file["size"],
                "required": file.get("required", True),
            }],
            file["file_dir"],
        )
        if not downloads:
            return False

        file["id"] = downloads[0]["id"]
        return True

    def handle_hard_error(self, file: dict, problems: list[dict]) -> bool:
        """Requeue a hard error, or fail the album when it cannot be retried."""
        if (
            len(problems) == len(self.required_files)
            or not self.cfg.slskd.requeue_failed_downloads
        ):
            return self.fail("Failed grab")

        file["retry"] = file.get("retry", 0) + 1
        if (
            file["retry"] > self.MAX_FILE_RETRIES
            or not self.requeue_file(file)
        ):
            return self.fail("Failed grab")

        return False

    def handle_rejected(self, file: dict, problems: list[dict]) -> bool:
        """Requeue a rejected transfer once the other transfers are stable."""
        required = self.required_files

        if (
            len(problems) == len(required)
            or not self.cfg.slskd.requeue_failed_downloads
        ):
            return self.fail("Failed grab")

        accounted = (
            sum(
                self.file_state(item) in self.STABLE
                for item in required
            )
            + len(problems)
        )
        if accounted < len(required):
            return False

        retry_limit = int(len(required) * 1.2)
        if (
            self.rejected_retries >= retry_limit
            or not self.requeue_file(file)
        ):
            return self.fail("Failed grab")

        self.rejected_retries += 1
        return False

    def handle_problems(self, problems: list[dict]) -> bool:
        """Handle current transfer errors, returning True if the album failed."""
        for file in problems:
            if (
                self.file_state(file) in self.HARD_ERRORS
                and self.handle_hard_error(file, problems)
            ):
                return True

        rejected = next(
            (
                file for file in problems
                if self.file_state(file) == "Completed, Rejected"
            ),
            None,
        )
        if rejected is not None:
            return self.handle_rejected(rejected, problems)

        return False

    def poll(self) -> bool:
        """Poll this album once, returning True when it is terminal."""
        elapsed = time.time() - self.count_start

        if not self.refresh_download_status():
            if elapsed >= self.cfg.slskd.stalled_timeout:
                return self.fail("Timeout waiting for download status")
            return False

        done, problems, remote_count = self.download_progress()

        if done:
            logger.info(f"Completed download of {self.artist} - {self.title}")
            self.discard_incomplete_optional_files()
            self.process_completed_album()
            return True

        if elapsed >= self.cfg.slskd.stalled_timeout:
            return self.fail("Timeout waiting for download")

        if (
            remote_count == len(self.required_files)
            and elapsed >= self.cfg.slskd.remote_queue_timeout
        ):
            return self.fail("Remote queue timeout")

        return self.handle_problems(problems)

    def move_failed_import(self, src_path) -> str | None:
        """Move a failed Lidarr import's folder into `cfg.slskd.failed_imports_dir`, avoiding name clashes."""
        failed_imports_dir = self.cfg.slskd.failed_imports_dir

        if not os.path.exists(failed_imports_dir):
            os.makedirs(failed_imports_dir)

        folder_name = os.path.basename(os.path.normpath(src_path))
        folder_path = safe_path(self.cfg.slskd.download_dir, folder_name)
        if folder_path is None:
            logger.error("Refusing to move a failed import from an invalid slskd download path")
            return None
        target_path = os.path.join(failed_imports_dir, folder_name)

        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.join(failed_imports_dir, f"{folder_name}_{counter}")
            counter += 1

        if os.path.exists(folder_path):
            shutil.move(folder_path, target_path)
            logger.info(f"Failed import moved to: {target_path}")
            return os.path.abspath(target_path)

        logger.warning(f"Failed import source folder not found: {folder_path}")
        return None

    def trigger_lidarr_import(self) -> None:
        """Ask Lidarr to scan the import folder, wait for the command to finish, and record failures."""

        command = lidarr.post_command(
            name="DownloadedAlbumsScan",
            path=self.import_folder,
        )  # Album all tagged up and in a correctly named folder. This should work more reliably
        logger.info(f"Starting Lidarr import for: {self.title} ID: {command['id']}")

        deadline = time.time() + self.cfg.lidarr.import_timeout
        while True:
            current_task = lidarr.get_command(command["id"])
            if current_task["status"] in ("completed", "failed"):
                break
            if time.time() >= deadline:
                logger.error(f"Timed out waiting for Lidarr import of {self.artist} - {self.title}")
                self.wanted_album.state.import_pending = True
                return
            time.sleep(2)

        failed = (
            not isinstance(current_task, dict)
            or current_task.get("status") == "failed"
            or current_task.get("result") == "unsuccessful"
        )
        if failed:
            self.wanted_album.failure = "Lidarr import failed"

        try:
            logger.info(f"{current_task['commandName']} {current_task['message']} from: {current_task['body']['path']}")
        except Exception:
            logger.exception("Error printing lidarr task message")
            logger.error(current_task)

        if failed:
            try:
                self.move_failed_import(current_task["body"]["path"])
            except Exception:
                logger.exception("Error moving failed Lidarr import")
            if self.cfg.lidarr.failed_import_denylist:
                self.wanted_album.add_to_failed_import_denylist()


@dataclass
class GrabbedAlbums:
    """A list of Slskd grabbed albums, with a few convenience methods for filtering and sorting."""
    albums: list[GrabbedAlbum] = field(default_factory=list)

    def __iter__(self) -> Iterator[GrabbedAlbum]:
        return iter(self.albums)

    def __len__(self) -> int:
        return len(self.albums)

    def append(self, album: GrabbedAlbum) -> None:
        self.albums.append(album)

    def monitor_downloads(self) -> None:
        """Poll active albums until every album reaches a terminal state."""
        while self.albums:
            for album in self.albums.copy():
                if album.poll():
                    self.albums.remove(album)

            if self.albums:
                time.sleep(5)


def sanitize_folder_name(folder_name):
    valid_characters = re.sub(r'[<>:."/\\|?*]', "", folder_name)
    return valid_characters.strip()


def safe_path(download_root: str, *parts: str) -> str | None:
    """Return a validated child path, or None for unsafe components."""
    if not parts or any(not part or part in (".", "..") or os.path.isabs(part) or "/" in part or "\\" in part for part in parts):
        return None

    try:
        root_path = os.path.realpath(download_root)
        candidate_path = os.path.realpath(os.path.join(root_path, *parts))
        return candidate_path if os.path.commonpath((root_path, candidate_path)) == root_path else None
    except ValueError:
        return None


def _cancel_and_delete_files(cfg: AppConfig, files: list[dict]) -> None:
    """Cancel each file's in-progress slskd download and remove its local file."""

    for file in files:
        try:
            slskd.transfers.cancel_download(username=file["username"], id=file["id"])
        except Exception:
            logger.warning(f"Failed to cancel download {file['filename']} for {file['username']}", exc_info=True)
        filename = file["filename"].split("\\")[-1]
        delete_dir = safe_path(cfg.slskd.download_dir, file["file_dir"].split("\\")[-1])
        delete_file = safe_path(delete_dir, filename) if delete_dir else None
        if delete_file and os.path.isfile(delete_file) and not os.path.islink(delete_file):
            try:
                os.remove(delete_file)
            except OSError:
                logger.warning(f"Failed to remove download file {delete_file}", exc_info=True)
        if delete_dir and os.path.isdir(delete_dir) and not os.path.islink(delete_dir):
            try:
                os.rmdir(delete_dir)
            except OSError:
                pass


def release_trackcount_mode(releases):
    counts = Counter(
        release["trackCount"]
        for release in releases
    )
    return counts.most_common(1)[0][0] if counts else None


def verify_filetype(file, allowed_filetype):
    """Check whether a slskd search result file matches an `allowed_filetypes` config entry.

    Matches on file extension, and if the config entry also specifies quality attributes
    (bitrate, or bitdepth/samplerate), verifies those against the file's metadata too.
    """
    current_filetype = file["filename"].split(".")[-1]
    allowed_parts = allowed_filetype.split(" ", 1)

    if current_filetype.lower() != allowed_parts[0].lower():
        return False
    if len(allowed_parts) == 1:
        return True  # No quality attributes specified, so the extension match is enough.

    selected_attributes = allowed_parts[1]

    # If it is a bitdepth/samplerate pair instead of a simple bitrate
    if "/" in selected_attributes:
        selected_bitdepth, selected_samplerate_raw = selected_attributes.split("/", 1)
        try:
            selected_samplerate = str(int(float(selected_samplerate_raw) * 1000))
        except ValueError:
            logger.warning("Invalid samplerate in selected_attributes")
            return False

        bitdepth = file.get("bitDepth")
        samplerate = file.get("sampleRate")
        if not bitdepth or not samplerate:
            return False
        return str(bitdepth) == str(selected_bitdepth) and str(samplerate) == str(selected_samplerate)

    # If it is a bitrate
    bitrate = file.get("bitRate")
    if not bitrate:
        return False
    return str(bitrate) == selected_attributes


def slskd_do_enqueue(username, files, file_dir):
    """Enqueue files for download from a user and return the ones slskd accepted.

    Each returned file dict is annotated with the tracking details (id, file_dir, username,
    size) needed to poll its download status later.
    """
    try:
        before = slskd.transfers.get_downloads(username=username)
    except Exception:
        logger.warning(f"Failed to snapshot existing downloads for {username} before enqueue", exc_info=True)
        return None

    before_directory = next((d for d in before["directories"] if d["directory"] == file_dir), {"files": []})
    existing_ids = {file["id"] for file in before_directory["files"] if "id" in file}

    try:
        enqueue_files = [{"filename": file["filename"], "size": file["size"]} for file in files]
        enqueue = slskd.transfers.enqueue(username=username, files=enqueue_files)
    except Exception:
        logger.debug("Enqueue failed", exc_info=True)
        return None
    if not enqueue:
        return None

    enqueue_records = (
        enqueue
        if isinstance(enqueue, list)
        else enqueue.get("files", [enqueue])
        if isinstance(enqueue, dict)
        else []
    )
    accepted_ids = {
        record["filename"]: record["id"]
        for record in enqueue_records
        if isinstance(record, dict) and "filename" in record and "id" in record
    }
    deadline = time.time() + TRANSFER_APPEAR_TIMEOUT
    while not all(file["filename"] in accepted_ids for file in files) and time.time() < deadline:
        try:
            download_list = slskd.transfers.get_downloads(username=username)
            directory = next((d for d in download_list["directories"] if d["directory"] == file_dir), None)
            if directory is not None:
                accepted_ids.update({
                    file["filename"]: file["id"]
                    for file in directory["files"]
                    if "id" in file and file["id"] not in existing_ids
                })
        except Exception:
            logger.warning(f"Failed to get download status for {username} after enqueue", exc_info=True)
        time.sleep(1)

    return [
        {
            "filename": file["filename"],
            "id": accepted_ids[file["filename"]],
            "file_dir": file_dir,
            "username": username,
            "size": file["size"],
            "required": file.get("required", True),
        }
        for file in files
        if file["filename"] in accepted_ids
    ]


def is_docker():
    return os.getenv("IN_DOCKER") is not None


def remove_lock_file(path: str) -> None:
    """Docker doesn't use a lock file, so only remove it outside Docker."""
    if not is_docker():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def acquire_lock_file(path: str):
    """Acquire an OS lock that is released automatically if the process crashes."""
    lock_file = open(path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        logger.info("Soulseekarr instance is already running.")
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def setup_logging(config: dict, var_dir: str) -> None:
    """Configure the root logger from the `logging` config section, resetting prior handlers.

    Always logs to stdout, and additionally to a rotating file in `var_dir` if `log_to_file`
    is enabled.
    """
    from logging.handlers import RotatingFileHandler

    log_config = config.get("logging") or DEFAULT_LOGGING_CONF

    level = log_config.get("level", DEFAULT_LOGGING_CONF["level"])
    fmt = log_config.get("format", DEFAULT_LOGGING_CONF["format"])
    datefmt = log_config.get("datefmt", DEFAULT_LOGGING_CONF["datefmt"])

    # force=True drops any handlers from a previous loop cycle so they don't stack up.
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, force=True)

    log_to_file = bool(log_config.get("log_to_file", True))
    if log_to_file:
        log_filename = log_config.get("log_file", "soulseekarr.log")
        log_file_path = os.path.join(var_dir, log_filename)
        max_bytes = int(log_config.get("max_bytes", 1048576))
        backup_count = int(log_config.get("backup_count", 3))

        file_handler = RotatingFileHandler(log_file_path, maxBytes=max_bytes, backupCount=backup_count)
        file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        logging.getLogger().addHandler(file_handler)
        logger.info(f"Logging to file: {log_file_path}")


def prune_wanted_record(raw: dict, source: AlbumSource) -> WantedAlbum:
    """Trim a raw Lidarr wanted-list record down to the fields Soulseekarr actually reads."""
    return WantedAlbum(
        id=raw["id"],
        artistId=raw["artistId"],
        artist=raw["artist"]["artistName"],
        title=raw["title"],
        year=raw["releaseDate"][0:4],
        source=source,
    )


def get_wanted_albums(cfg: AppConfig) -> WantedAlbums:
    """Return the next batch of wanted albums for the active source configuration.

    Reuses albums left over from the last fetch. When none remain, fetches Lidarr's complete
    wanted list, removes albums already in Lidarr's download queue, and saves the unreturned
    albums for later runs.
    """

    remaining_albums = remaining_albums_by_source.get(cfg.source, WantedAlbums())
    fetched_wanted_list = False

    if not remaining_albums:
        fetched_wanted_list = True
        if cfg.lidarr.search_type not in ("incrementing", "random"):
            raise ValueError(f"[lidarr.search_type] - {cfg.lidarr.search_type = } is not valid")

        wanted_kwargs = {
            "missing": cfg.source == "missing",
            # Page size used for the internal Lidarr API calls that bulk-fetch the wanted list.
            # Unrelated to lidarr.chunk_size, which controls how many records get processed per run.
            "page_size": 250, 
            "sort_key": "id" if cfg.lidarr.search_type == "random" else cfg.lidarr.sort_key,
            "sort_dir": "ascending" if cfg.lidarr.search_type == "random" else cfg.lidarr.sort_dir,
        }
        try:
            wanted_page = lidarr.get_wanted(page=1, **wanted_kwargs)
        except PyarrError as ex:
            logger.error(f"An error occurred when attempting to get records: {ex}")
            return WantedAlbums()

        raw_albums = list(wanted_page["records"])
        total_albums = wanted_page["totalRecords"]
        page = 2
        while len(raw_albums) < total_albums:
            try:
                wanted_page = lidarr.get_wanted(page=page, **wanted_kwargs)
            except PyarrError as ex:
                logger.error(f"Failed to grab record: {ex}")
                break

            page_records = wanted_page["records"]
            if not page_records:
                logger.warning("Lidarr returned an empty page before totalRecords was reached")
                break

            raw_albums.extend(page_records)
            page += 1

        remaining_albums = filter_queued_albums(
            WantedAlbums(
                albums=[prune_wanted_record(raw, cfg.source) for raw in raw_albums]
            )
        )
        if cfg.lidarr.search_type == "random":
            remaining_albums.shuffle()

    batch_size = max(1, cfg.lidarr.chunk_size)
    albums_to_process = remaining_albums.take(batch_size)
    if not fetched_wanted_list:
        albums_to_process = filter_queued_albums(albums_to_process)
    remaining_albums_by_source[cfg.source] = remaining_albums
    return albums_to_process


def filter_queued_albums(albums: WantedAlbums) -> WantedAlbums:
    """Drop any wanted record whose album is already in Lidarr's download queue."""
    if not albums:
        return albums

    try:
        queued_albums = lidarr.get_queue(sort_dir="ascending", sort_key="albums.title")
        total_queued = queued_albums["totalRecords"]
        current_queue = queued_albums["records"]

        if queued_albums["pageSize"] < total_queued:
            page = 2
            while len(current_queue) < total_queued:
                try:
                    next_page = lidarr.get_queue(page=page, sort_key="albums.title", sort_dir="ascending")
                except PyarrError as ex:
                    logger.error(f"Failed to get queue details: {ex}")
                    break
                page_records = next_page["records"]
                if not page_records:
                    logger.warning("Lidarr returned an empty page before totalRecords was reached")
                    break
                current_queue.extend(page_records)
                page += 1

        queued_album_ids = set()
        for album in current_queue:
            if "albumId" in album:
                queued_album_ids.add(album["albumId"])
            else:
                logger.warning(f"Dropping entry due to missing key in keylist: [{album.keys()}]")

        not_queued = WantedAlbums()
        for album in albums:
            if album.id in queued_album_ids:
                logger.info(f"Skipping album '{album.title}' because it's already in download queue")
                continue
            not_queued.append(album)
        return not_queued
    except PyarrError as ex:
        logger.error(f"Failed to get queue details so not filtering based on queue: {ex}")
        return albums


def main():
    """Parse CLI arguments, resolve configuration, then run Soulseekarr once or on a loop."""
    global lidarr, slskd, source_configs, album_states, remaining_albums_by_source
    parser = argparse.ArgumentParser(description="""Soulseekarr reads all of your "wanted" albums/artists from Lidarr and downloads them using Slskd""")
    default_data_directory = "/data" if is_docker() else os.getcwd()
    parser.add_argument("-c", "--config-dir", default=default_data_directory, const=default_data_directory, nargs="?", type=str, help="Config directory (default: %(default)s)")
    parser.add_argument("-v", "--var-dir", default=default_data_directory, const=default_data_directory, nargs="?", type=str, help="Var directory (default: %(default)s)")
    parser.add_argument("--no-lock-file", action="store_false", dest="lock_file", default=True, help="Disable lock file creation")
    parser.add_argument("--interval", type=int, default=None, help="Seconds to wait between runs, overriding LOOP_INTERVAL and config.yml. Use 0 to run once.")
    args = parser.parse_args()

    lock_file_path = os.path.join(args.var_dir, ".soulseekarr.lock")
    config_file_path = os.path.join(args.config_dir, "config.yml")
    lock_file = None
    try:
        if not is_docker() and args.lock_file:
            lock_file = acquire_lock_file(lock_file_path)
            if lock_file is None:
                return
        if not os.path.exists(config_file_path) and migrate_soularr_ini_config(args.config_dir):
            return
        if not os.path.exists(config_file_path):
            logger.error('Config file does not exist! Please mount "/data" and place your "config.yml" file there.' if is_docker() else "Config file does not exist! Please place it in the working directory.")
            return

        with open(config_file_path, "r") as config_file:
            raw_config = expand_env_vars(yaml.safe_load(config_file) or {})
        setup_logging(raw_config, args.var_dir)
        cfg = AppConfig.from_yaml(raw_config, args)
        interval: int = cfg.interval
        slskd = slskd_api.SlskdClient(host=cfg.slskd.host_url, api_key=cfg.slskd.api_key, url_base=cfg.slskd.url_base)
        lidarr = LidarrAPI(urljoin(f"{cfg.lidarr.host_url.rstrip('/')}/", cfg.lidarr.url_base.strip("/")), cfg.lidarr.api_key)
        source_configs = {source: AppConfig.from_yaml(raw_config, args, source=source) for source in cfg.lidarr.sources}
        album_states = {}
        remaining_albums_by_source = {}

        while True:
            wanted_albums = WantedAlbums()
            for source, config in source_configs.items():
                logger.info(f"Getting wanted albums from '{source}' list")
                try:
                    albums_to_append = get_wanted_albums(config)
                    if albums_to_append:
                        logger.info(f"Fetched {len(albums_to_append)} albums from '{source}' list that aren't on the deny list and/or blacklisted.")
                        wanted_albums.extend(albums_to_append)
                    else:
                        logger.info(f"No albums fetched from '{source}' list that aren't on the deny list and/or blacklisted.")
                except ValueError as ex:
                    logger.error(f"An error occurred: {ex}")
                    time.sleep(interval)

            wanted_albums = wanted_albums.deduplicate()
            if not wanted_albums:
                logger.info("No wanted albums available.")
                if interval <= 0:
                    break
                time.sleep(interval)
                continue

            logger.info(f"Total wanted albums to process: {len(wanted_albums)}")
            try:
                filtered_wanted_albums = wanted_albums.filter_list()
                if filtered_wanted_albums:
                    total_failed = filtered_wanted_albums.grab_most_wanted()
                    if total_failed == 0:
                        logger.info("Soulseekarr finished.")
                    else:
                        logger.info(f"{total_failed}: releases failed to find a match in the search results and are still wanted.")
                else:
                    logger.info("No releases fetched that aren't on the deny list and/or blacklisted.")
                if cfg.slskd.remove_completed_downloads:
                    slskd.transfers.remove_completed_downloads()
            except Exception:
                logger.exception("Fatal error!")
                return 1

            if interval > 0:
                time.sleep(interval)
            else:
                break
    finally:
        if lock_file is not None:
            lock_file.close()
            remove_lock_file(lock_file_path)


if __name__ == "__main__":
    sys.exit(main())
