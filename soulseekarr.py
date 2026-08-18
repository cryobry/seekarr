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
from collections import Counter, deque
from requests.exceptions import HTTPError, RequestException
from pyarr import LidarrAPI
from pyarr.exceptions import PyarrError
from migration import expand_env_vars, migrate_soularr_ini_config

logger = logging.getLogger("soulseekarr")

DEFAULT_LOGGING = {
    "level": "INFO",
    "format": "[%(levelname)s|%(name)s|L%(lineno)d] %(asctime)s: %(message)s",
    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
    "log_to_file": True,
    "log_file": "soulseekarr.log",
    "max_bytes": 1048576,
    "backup_count": 3,
}

AlbumSource = Literal["missing", "cutoff_unmet"]


@dataclass
class DownloadSource:
    """A complete directory source for one candidate album attempt."""
    username: str
    file_dir: str
    files: list[dict]
    required_files: list[dict]
    has_free_upload_slot: bool
    upload_speed: float
    queue_length: int
    disk_no: int | None = None
    disk_count: int | None = None


@dataclass
class DownloadCandidate:
    """A validated album release that can be enqueued after search cleanup."""
    filetype: str
    release_id: int
    release_format: str
    sources: list[DownloadSource]
    match_score: float
    has_free_upload_slot: bool
    upload_speed: float
    queue_length: int
    filetype_rank: int
    release_rank: int


@dataclass
class AlbumState:
    """State shared by every instance representing the same Lidarr album."""
    queued: bool = False
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
    shuffle_all: bool
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
    monitor_downloads: bool
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
        search_type = str(
            env_override(
                "lidarr",
                "search_type",
                resolved_lidarr.get("search_type", "incrementing"),
            )
        ).lower().strip()
        if search_type not in ("incrementing", "shuffle"):
            raise ValueError(f"[lidarr.search_type] - {search_type = } is not valid")
        lidarr = LidarrConfig(
            api_key=env_override("lidarr", "api_key", resolved_lidarr.get("api_key")),
            host_url=env_override("lidarr", "host_url", resolved_lidarr.get("host_url")),
            url_base=str(env_override("lidarr", "url_base", resolved_lidarr.get("url_base", "/"))),
            download_dir=env_override("lidarr", "download_dir", resolved_lidarr.get("download_dir")),
            import_timeout=int(env_override("lidarr", "import_timeout", resolved_lidarr.get("import_timeout", 3600))),
            disable_sync=bool(env_override("lidarr", "disable_sync", resolved_lidarr.get("disable_sync", False))),
            sources=sources,
            search_type=search_type,
            shuffle_all=bool(env_override("lidarr", "shuffle_all", lidarr_cfg.get("shuffle_all", False))),
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
                env_override("slskd", "failed_imports_dir", resolved_slskd.get("failed_imports_dir") or os.path.join(slskd_download_dir, ".failed_imports"))
            ),
            url_base=str(env_override("slskd", "url_base", resolved_slskd.get("url_base", "/"))),
            monitor_downloads=bool(
                env_override(
                    "slskd",
                    "monitor_downloads",
                    resolved_slskd.get("monitor_downloads", True),
                )
            ),
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
    libraries.
    """
    failure: str | None = field(default=None, init=False)
    search_id: str | None = field(default=None, init=False, repr=False)
    search_deadline: float | None = field(default=None, init=False, repr=False)
    search_results: dict = field(default_factory=dict, init=False, repr=False)
    folder_cache: dict = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def prune_wanted_record(cls, raw: dict, source: AlbumSource) -> "WantedAlbum":
        """Build a wanted album from the fields used by Soulseekarr."""
        return cls(
            id=raw["id"],
            artistId=raw["artistId"],
            artist=raw["artist"]["artistName"],
            title=raw["title"],
            year=raw["releaseDate"][0:4],
            source=source,
        )

    def skip_reason(self) -> str | None:
        """Return why this album should be skipped, or None if it is eligible."""
        if self.state.queued:
            return "already queued"
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

    def start_search(self) -> bool:
        """Start this album's slskd search without waiting for results."""
        self.failure = None
        self.search_id = None
        self.search_deadline = None

        include_artist = len(self.title) == 1 or self.cfg.slskd.album_prepend_artist
        query = f"{self.artist} {self.title}" if include_artist else self.title
        original_query = query

        for word in filter(None, self.cfg.slskd.search_blacklist):
            query = re.sub(re.escape(word), "", query, flags=re.IGNORECASE)

        query = " ".join(query.split())

        if query != original_query:
            logger.info(f"Filtered search query: '{original_query}' -> '{query}'")
        if not query:
            logger.warning(f"Skipping {self.artist} - {self.title}: empty search query")
            self.failure = "Search failed"
            return False

        logger.info(f"Searching for '{self.source}' album: {self.artist} - {self.title}")
        logger.debug(f"Search query: '{query}'")

        timeout = max(1, self.cfg.slskd.timeout)

        try:
            search = slskd.searches.search_text(
                searchText=query,
                searchTimeout=timeout * 1000,
                filterResponses=True,
                maximumPeerQueueLength=self.cfg.slskd.maximum_peer_queue,
                minimumPeerUploadSpeed=self.cfg.slskd.minimum_peer_upload_speed,
            )
            self.search_id = search["id"]
            return True

        except Exception:
            self.failure = "Search failed"
            logger.exception(f"Failed to start search via SLSKD: {query}")
            return False

    def search_finished(self, state: dict | None = None) -> bool:
        """Return whether this search is complete or ready for timeout cleanup."""
        if self.search_id is None:
            return True

        if state is None:
            try:
                state = slskd.searches.state(self.search_id, False)
            except Exception:
                logger.exception(
                    f"Failed to check SLSKD search for {self.artist} - {self.title}"
                )
                return True

        if state["state"] == "InProgress" and self.search_deadline is None:
            self.search_deadline = (
                time.monotonic() + max(1, self.cfg.slskd.timeout) + 5
            )

        return state["isComplete"] or bool(
            self.search_deadline
            and time.monotonic() >= self.search_deadline
        )

    def collect_search(self) -> bool:
        """Cache this search's results and remove it from SLSKD."""
        search_id = self.search_id
        try:
            success = self.search_for_album()
            if not success:
                self.failure = "Search failed"
            return success
        finally:
            self.search_id = None
            self.search_deadline = None

            if search_id and self.cfg.slskd.delete_searches:
                try:
                    if not slskd.searches.delete(search_id):
                        logger.warning(f"SLSKD did not delete search {search_id}")
                except Exception:
                    logger.warning(
                        f"Failed to delete search {search_id} from SLSKD",
                        exc_info=True,
                    )

    def queue_download(self) -> GrabbedAlbum | None:
        """Enqueue the best match from this album's cached search results."""
        try:
            if self.search_id is not None and not self.collect_search():
                return None

            if not self.search_results:
                self.failure = "Search failed"
                return None

            download = self.find_download()
            if download is None:
                self.failure = "No matching download"

            return download
        finally:
            self.search_results.clear()
            self.folder_cache.clear()

    def search_for_album(self) -> bool:
        """Wait for the previously started search and cache its results."""
        if self.search_id is None:
            logger.error(
                f"No SLSKD search was started for {self.artist} - {self.title}"
            )
            return False

        try:
            while not self.search_finished():
                time.sleep(1)

            state = slskd.searches.state(self.search_id, False)
            if not state["isComplete"]:
                logger.warning(
                    f"SLSKD search for {self.artist} - {self.title} did not "
                    f"complete within its {self.cfg.slskd.timeout}s timeout "
                    f"+ 5s grace; stopping it and using partial results"
                )
                slskd.searches.stop(self.search_id)

            responses = slskd.searches.search_responses(self.search_id)

        except Exception:
            logger.exception(
                f"Failed to collect SLSKD search for "
                f"{self.artist} - {self.title}"
            )
            return False

        logger.info(
            f"Search for {self.artist} - {self.title} "
            f"returned {len(responses)} results"
        )
        if not responses:
            return False

        self.search_results.clear()

        for result in responses:
            username = result["username"]
            if username in self.cfg.slskd.ignored_users:
                logger.debug(f"Skipping ignored user: {username}")
                continue
            user_results = self.search_results.setdefault(username, {})
            logger.debug(f"Caching and truncating results for user: {username}")

            peer = {"hasFreeUploadSlot": bool(result.get("hasFreeUploadSlot", False)),
                    "uploadSpeed": result.get("uploadSpeed") or 0,
                    "queueLength": result.get("queueLength") or 0}

            for file in result["files"]:
                file_dir = file["filename"].rsplit("\\", 1)[0]

                for filetype in self.cfg.slskd.allowed_filetypes:
                    if verify_filetype(file, filetype):
                        directories = user_results.setdefault(filetype, [])
                        if not any(item["file_dir"] == file_dir for item in directories):
                            directories.append({"file_dir": file_dir, **peer})

        return True

    def find_download(self) -> GrabbedAlbum | None:
        """Rank every complete match, then enqueue the first usable candidate."""
        candidates = self.discover_candidates()
        candidates.sort(key=self.candidate_sort_key)

        for rank, candidate in enumerate(candidates, 1):
            downloads = self.enqueue_candidate(candidate)
            if downloads:
                self.log_selected_candidate(candidate, rank)
                return GrabbedAlbum(
                    wanted_album=self,
                    files=downloads,
                    candidate=candidate,
                    fallback_candidates=deque(candidates[rank:]),
                )

        return None

    def discover_candidates(self) -> list[DownloadCandidate]:
        """Discover every complete candidate without starting any downloads."""
        results = self.search_results
        releases = lidarr.get_album(self.id)["releases"]
        tracks_by_release = {}
        ordered_releases = []
        remaining = list(releases)
        while remaining:
            release = self.choose_release(remaining)
            if release is None:
                break
            remaining.remove(release)
            ordered_releases.append(release)

        candidates = []

        for filetype_rank, filetype in enumerate(self.cfg.slskd.allowed_filetypes):
            if not any(filetype in result for result in results.values()):
                logger.debug(f"No search results for {filetype}. Skipping.")
                continue

            for release_rank, release in enumerate(ordered_releases):
                release_id = release["id"]
                if release_id not in tracks_by_release:
                    tracks_by_release[release_id] = lidarr.get_tracks(
                        artistId=self.artistId,
                        albumId=self.id,
                        albumReleaseId=release_id,
                    )
                tracks = tracks_by_release[release_id]

                if len(release["media"]) > 1:
                    candidates.extend(
                        self.discover_multi_candidates(
                            release,
                            tracks,
                            results,
                            filetype,
                            filetype_rank,
                            release_rank,
                        )
                    )
                else:
                    candidates.extend(
                        self.discover_single_candidates(
                            release,
                            tracks,
                            results,
                            filetype,
                            filetype_rank,
                            release_rank,
                        )
                    )

        return candidates

    @staticmethod
    def candidate_sort_key(candidate: DownloadCandidate):
        """Keep configured format/release preference ahead of match quality."""
        return (
            candidate.filetype_rank,
            candidate.release_rank,
            -candidate.match_score,
            not candidate.has_free_upload_slot,
            -candidate.upload_speed,
            candidate.queue_length,
        )

    def discover_single_candidates(
        self,
        release,
        tracks,
        results,
        filetype,
        filetype_rank,
        release_rank,
    ) -> list[DownloadCandidate]:
        candidates = []
        for username, user_results in results.items():
            for result in user_results.get(filetype, []):
                logger.debug(f"Parsing result from user: {username}")
                match = self.check_for_match(
                    tracks,
                    filetype,
                    [result["file_dir"]],
                    username,
                )
                if match:
                    directory, file_dir, required_files, match_score = match
                    source = self.make_source(username, result, directory, file_dir, required_files)
                    candidates.append(self.make_candidate(
                        release, filetype, filetype_rank, release_rank, [source], match_score
                    ))

        return candidates

    def discover_multi_candidates(
        self,
        release,
        tracks,
        results,
        filetype,
        filetype_rank,
        release_rank,
    ) -> list[DownloadCandidate]:
        """Discover complete combinations of distinct sources for every disc."""
        media = release["media"]
        if not self.cfg.lidarr.allow_multi_disc or len(media) <= 1:
            return []

        matches_by_disc = []
        for medium in media:
            disk_no = medium["mediumNumber"]
            disk_tracks = [
                track for track in tracks
                if track["mediumNumber"] == disk_no
            ]
            disk_matches = []
            for username, user_results in results.items():
                for result in user_results.get(filetype, []):
                    match = self.check_for_match(
                        disk_tracks,
                        filetype,
                        [result["file_dir"]],
                        username,
                    )
                    if match:
                        directory, file_dir, required_files, match_score = match
                        disk_matches.append({"username": username, "result": result,
                                              "directory": directory, "file_dir": file_dir,
                                              "required_files": required_files,
                                              "match_score": match_score, "disk_no": disk_no})
            if not disk_matches:
                return []
            matches_by_disc.append(disk_matches)

        candidates = []

        def collect(index, sources, used_sources):
            if index == len(matches_by_disc):
                candidate_sources = [self.make_source(
                    match["username"], match["result"], match["directory"],
                    match["file_dir"], match["required_files"], match["disk_no"], len(media)
                ) for match in sources]
                score = sum(match["match_score"] for match in sources) / len(sources)
                candidates.append(self.make_candidate(
                    release, filetype, filetype_rank, release_rank, candidate_sources, score
                ))
                return

            for match in matches_by_disc[index]:
                source_key = (match["username"], match["file_dir"])
                if source_key in used_sources:
                    continue
                collect(index + 1, sources + [match], used_sources | {source_key})

        collect(0, [], set())
        return candidates

    @staticmethod
    def make_source(
        username,
        peer,
        directory,
        file_dir,
        required_files,
        disk_no=None,
        disk_count=None,
    ) -> DownloadSource:
        return DownloadSource(username, file_dir, copy.deepcopy(directory["files"]),
                              copy.deepcopy(required_files),
                              bool(peer.get("hasFreeUploadSlot", False)),
                              float(peer.get("uploadSpeed") or 0),
                              int(peer.get("queueLength") or 0), disk_no, disk_count)

    @staticmethod
    def make_candidate(
        release,
        filetype,
        filetype_rank,
        release_rank,
        sources,
        match_score,
    ) -> DownloadCandidate:
        return DownloadCandidate(
            filetype, release["id"], release["format"], sources, match_score,
            all(source.has_free_upload_slot for source in sources),
            min(source.upload_speed for source in sources),
            max(source.queue_length for source in sources), filetype_rank, release_rank,
        )

    def enqueue_candidate(self, candidate: DownloadCandidate) -> list[dict] | None:
        """Enqueue every source in a candidate, removing partial attempts."""
        downloads = []
        for source in candidate.sources:
            source_downloads = self._enqueue_match(source)
            if not source_downloads:
                if downloads:
                    self.cancel_and_delete(downloads, remove=True)
                return None
            downloads.extend(source_downloads)
        return downloads

    def log_selected_candidate(self, candidate: DownloadCandidate, rank: int) -> None:
        locations = ", ".join(
            f"{source.username}\\{source.file_dir}"
            for source in candidate.sources
        )
        logger.info(
            f"Selected candidate {rank} for {self.artist} - {self.title}: "
            f"{locations}, score={candidate.match_score:.3f}, "
            f"free-slot={candidate.has_free_upload_slot}, "
            f"upload-speed={candidate.upload_speed:g}, "
            f"queue-length={candidate.queue_length}"
        )

    def _enqueue_match(self, source: DownloadSource):
        """Enqueue an already validated album directory."""
        files = {
            file["filename"]: copy.deepcopy(file)
            for file in source.files
        }
        files.update({
            file["filename"]: copy.deepcopy(file)
            for file in source.required_files
        })
        required_names = {file["filename"] for file in source.required_files}

        for filename, file in files.items():
            file["required"] = filename in required_names
            file["filename"] = f"{source.file_dir}\\{filename}"

        try:
            downloads = slskd_do_enqueue(
                source.username,
                list(files.values()),
                source.file_dir,
            )
        except Exception:
            logger.exception(
                f"Exception enqueueing {self.artist} - "
                f"{self.title} from {source.username}"
            )
            return None

        accepted = {file["filename"] for file in downloads or []}
        required = {
            file["filename"] for file in files.values()
            if file["required"]
        }

        if downloads and required <= accepted:
            for file in downloads:
                if source.disk_no is not None:
                    file["disk_no"] = source.disk_no
                    file["disk_count"] = source.disk_count
            return downloads
        if downloads:
            self.cancel_and_delete(downloads, remove=True)

        logger.info(f"Failed to enqueue {self.artist} - {self.title} from {source.username}")
        return None

    def check_for_match(self, tracks, filetype, file_dirs, username):
        """Return the first reasonably sized directory containing every required track."""
        user_cache = self.folder_cache.setdefault(username, {})
        track_count = len(tracks)
        maximum_audio_files = max(track_count * 2, track_count + 10)
        maximum_files = maximum_audio_files + 25

        for file_dir in file_dirs:
            if file_dir in user_cache:
                directory = copy.deepcopy(user_cache[file_dir])
            else:
                try:
                    directory = slskd.users.directory(username=username, directory=file_dir)[0]
                except HTTPError as ex:
                    status = (ex.response.status_code if ex.response is not None else "unknown")
                    logger.warning(f'HTTP error reading "{username}\\{file_dir}": {status}')
                    if ex.response is not None and ex.response.text:
                        logger.debug(f"SLSKD response body: {ex.response.text[:500]}")
                    continue
                except IndexError:
                    logger.warning(f'Empty directory response for "{username}\\{file_dir}"')
                    directory = {"files": []}
                except RequestException:
                    logger.exception(f'Network error reading directory from "{username}"')
                    return None
                except Exception:
                    logger.exception(f'Error reading directory from "{username}"')
                    return None

                user_cache[file_dir] = copy.deepcopy(directory)

            audio_files = [
                file for file in directory["files"]
                if verify_filetype(file, filetype)
            ]

            if len(audio_files) < track_count:
                continue

            if len(audio_files) > maximum_audio_files:
                logger.debug(
                    f'Skipping oversized directory "{username}\\{file_dir}": '
                    f"{len(audio_files)} {filetype} files for "
                    f"{track_count} expected tracks"
                )
                continue

            directory = self.download_filter(filetype, directory)
            if len(directory["files"]) > maximum_files:
                logger.warning(
                    f'Rejecting oversized match for {self.artist} - {self.title} '
                    f'from "{username}\\{file_dir}": '
                    f'{len(directory["files"])} filtered files for '
                    f"{track_count} expected tracks"
                )
                continue

            matched = self.album_match(tracks, audio_files, username, filetype)
            if matched:
                required_files, match_score = matched
                return directory, file_dir, required_files, match_score

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

        if matched_files:
            return matched_files, total_match / len(matched_files)

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

    def cancel_and_delete(self, files, remove: bool = False) -> None:
        """Cancel downloads and remove their local files."""
        for file in files:
            try:
                cancel_args = {"username": file["username"], "id": file["id"]}
                if remove:
                    cancel_args["remove"] = True
                slskd.transfers.cancel_download(**cancel_args)
            except Exception:
                logger.warning(
                    f"Failed to cancel download {file['filename']} "
                    f"for {file['username']}",
                    exc_info=True,
                )

            filename = file["filename"].split("\\")[-1]
            folder = file["file_dir"].split("\\")[-1]
            delete_dir = safe_path(self.cfg.slskd.download_dir, folder)
            delete_file = safe_path(delete_dir, filename) if delete_dir else None

            if (
                delete_file
                and os.path.isfile(delete_file)
                and not os.path.islink(delete_file)
            ):
                try:
                    os.remove(delete_file)
                except OSError:
                    logger.warning(
                        f"Failed to remove download file {delete_file}",
                        exc_info=True,
                    )

            if (
                delete_dir
                and os.path.isdir(delete_dir)
                and not os.path.islink(delete_dir)
            ):
                try:
                    os.rmdir(delete_dir)
                except OSError:
                    pass

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
        """Return the first selected or eligible Lidarr release."""
        cfg = self.cfg.lidarr

        def is_multi_disc(release):
            return len(release.get("media", [])) > 1 or release.get("mediumCount", 1) > 1

        if cfg.use_selected_lidarr_release:
            selected = next(
                (
                    release
                    for release in releases
                    if release.get("monitored")
                    and (cfg.allow_multi_disc or not is_multi_disc(release))
                ),
                None,
            )
            if selected is not None:
                logger.info(f"Using selected Lidarr release for {self.artist}: {selected['format']}, {selected['trackCount']} tracks, ID: {selected['id']}")
                return selected

        def eligible(release):
            country = release["country"][0] if release["country"] else None
            return (
                (cfg.allow_multi_disc or not is_multi_disc(release))
                and (cfg.skip_region_check or country in cfg.accepted_countries)
                and self.release_format_accepted(release)
                and release["status"] == "Official"
            )

        eligible_releases = [release for release in releases if eligible(release)]
        if cfg.use_most_common_tracknum and eligible_releases:
            counts = Counter(release["trackCount"] for release in eligible_releases)
            track_count = counts.most_common(1)[0][0]
            eligible_releases = [release for release in eligible_releases if release["trackCount"] == track_count]

        if not eligible_releases:
            return None

        release = eligible_releases[0]
        country = release["country"][0] if release["country"] else None
        logger.info(f"Selected release for {self.artist}: {release['status']}, {country}, {release['format']}, Mediums: {release['mediumCount']}, Tracks: {release['trackCount']}, ID: {release['id']}")
        return release

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

    def filter_eligible(self) -> Self:
        """Return albums that are eligible to be searched."""
        filtered = []

        for album in self.albums:
            reason = album.skip_reason()
            if reason:
                logger.info(f"Skipping {reason}: {album.artist} - {album.title} (ID: {album.id})")
            else:
                filtered.append(album)

        return type(self)(albums=filtered)

    @classmethod
    def get_wanted(cls, cfg: AppConfig) -> Self:
        """Return the next batch of eligible wanted albums for this source."""
        remaining_albums = remaining_albums_by_source.get(cfg.source, cls())

        if not remaining_albums:
            wanted_kwargs = {
                "missing": cfg.source == "missing",
                "page_size": 250,
                "sort_key": "id" if cfg.lidarr.search_type == "shuffle" else cfg.lidarr.sort_key,
                "sort_dir": "ascending" if cfg.lidarr.search_type == "shuffle" else cfg.lidarr.sort_dir,
            }
            try:
                wanted_page = lidarr.get_wanted(page=1, **wanted_kwargs)
            except PyarrError as ex:
                logger.error(f"Failed to get wanted records: {ex}")
                return cls()

            raw_albums = list(wanted_page["records"])
            total_albums = wanted_page["totalRecords"]
            page = 2
            while len(raw_albums) < total_albums:
                try:
                    wanted_page = lidarr.get_wanted(page=page, **wanted_kwargs)
                except PyarrError as ex:
                    logger.error(f"Failed to get wanted page {page}: {ex}")
                    break

                page_records = wanted_page["records"]
                if not page_records:
                    logger.warning("Lidarr returned an empty page before totalRecords was reached")
                    break

                raw_albums.extend(page_records)
                page += 1

            remaining_albums = cls(
                albums=[WantedAlbum.prune_wanted_record(raw, cfg.source) for raw in raw_albums]
            )

            if cfg.lidarr.search_type == "shuffle" and not cfg.lidarr.shuffle_all:
                logger.info(f"search_type: shuffle (shuffling '{cfg.source}' albums)")
                remaining_albums.shuffle()

        remaining_albums = remaining_albums.filter_queued().filter_eligible()
        albums_to_process = remaining_albums.take(max(1, cfg.lidarr.chunk_size))
        remaining_albums_by_source[cfg.source] = remaining_albums
        return albums_to_process

    def grab_most_wanted(self, monitor_downloads: bool = True) -> int:
        """Search and enqueue every album, optionally waiting for completion."""
        downloads = self.search_and_queue()

        logger.info(f"Total downloads added: {len(downloads)}")
        for album in downloads:
            logger.info(f"Album: {album.title} Artist: {album.artist}")
        if monitor_downloads:
            downloads.monitor_downloads()
        else:
            for download in downloads:
                download.wanted_album.state.queued = True
            logger.info(
                "Download monitoring disabled; continuing to the next Lidarr batch"
            )

        failures = [album for album in self.albums if album.failure]
        logger.info(f"Failed albums: {len(failures)}")

        for album in failures:
            logger.info(f"{album.failure}: {album.artist} - {album.title}")

        return len(failures)

    def search_and_queue(self) -> GrabbedAlbums:
        """Submit spaced searches and process each one as it finishes."""
        pending: list[WantedAlbum] = []
        searched: deque[WantedAlbum] = deque()
        downloads = GrabbedAlbums()
        last_search_start: float | None = None
        last_interval = 0

        def collect_finished() -> bool:
            collected = False

            try:
                states = {
                    search["id"]: search
                    for search in slskd.searches.get_all()
                }
            except Exception:
                logger.exception("Failed to get SLSKD search states")
                return False

            for pending_album in pending.copy():
                if not pending_album.search_finished(
                    states.get(pending_album.search_id)
                ):
                    continue

                pending.remove(pending_album)
                if pending_album.collect_search():
                    searched.append(pending_album)
                collected = True

            return collected

        for album in self.albums:
            interval = max(0, album.cfg.slskd.minimum_search_interval)

            if last_search_start is not None:
                # Using the larger interval also handles differing per-source overrides.
                required_interval = max(last_interval, interval)
                next_search = last_search_start + required_interval

                while (remaining := next_search - time.monotonic()) > 0:
                    collect_finished()
                    time.sleep(min(1, remaining))

            last_search_start = time.monotonic()
            last_interval = interval

            if album.start_search():
                pending.append(album)

        while pending or searched:
            collect_finished()

            if searched:
                download = searched.popleft().queue_download()
                if download:
                    downloads.append(download)
            elif pending:
                time.sleep(1)

        return downloads

    def filter_queued(self) -> Self:
        """Return albums that are not already in Lidarr's download queue."""
        if not self.albums:
            return self

        current_queue = []
        total_queued = 1
        page = 1

        try:
            while len(current_queue) < total_queued:
                queue_page = lidarr.get_queue(
                    page=page,
                    sort_key="albums.title",
                    sort_dir="ascending",
                )
                page_records = queue_page["records"]
                total_queued = queue_page["totalRecords"]

                if not page_records:
                    if len(current_queue) < total_queued:
                        logger.warning(
                            "Lidarr returned an empty queue page before "
                            "totalRecords was reached, so the queue was not filtered"
                        )
                        return self
                    break

                current_queue.extend(page_records)
                page += 1
        except PyarrError as ex:
            logger.error(f"Failed to get queue details, so the queue was not filtered: {ex}")
            return self

        queued_ids = set()

        for record in current_queue:
            record_ids = {
                release["albumId"]
                for release in record.get("releases", [])
                if "albumId" in release
            }
            if "albumId" in record:
                record_ids.add(record["albumId"])

            if not record_ids:
                logger.warning(f"Ignoring queue entry without albumId: {record.keys()}")

            queued_ids.update(record_ids)

        not_queued = []
        for album in self.albums:
            if album.id in queued_ids:
                logger.info(f"Skipping album '{album.title}' because it's already in download queue")
            else:
                not_queued.append(album)

        return type(self)(albums=not_queued)

@dataclass
class GrabbedAlbum:
    """An album's enqueued downloads, tracked from enqueue through Lidarr import.

    Built once by find_download(), then mutated in place by monitor_downloads() and
    process_completed_album()/trigger_lidarr_import() as the download/import progresses.
    Identity fields (id, artist, title, ...) are read from `wanted_album` instead of being
    duplicated here.
    """
    SUCCESS = "Completed, Succeeded"
    REMOTE = "Queued, Remotely"
    ERRORS = {
        "Completed, Rejected",
        "Completed, Cancelled",
        "Completed, TimedOut",
        "Completed, Errored",
        "Completed, Aborted",
    }

    wanted_album: WantedAlbum
    files: list[dict]
    candidate: DownloadCandidate | None = None
    fallback_candidates: deque[DownloadCandidate] = field(default_factory=deque)
    count_start: float = field(default_factory=time.monotonic)
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
        """Prepare a completed download and optionally import it into Lidarr."""
        if self.cfg.slskd.rename_download_folders:
            folder_name = sanitize_folder_name(f"{self.artist} - {self.title} ({self.year})")
        else:
            folder_name = self.files[0]["file_dir"].split("\\")[-1]

        self.staging_folder = safe_path(self.cfg.slskd.download_dir, folder_name)
        if self.staging_folder is None:
            logger.error("Refusing to use an invalid slskd download directory")
            self.wanted_album.failure = "Download processing failed"
            return

        self.import_folder = os.path.join(self.cfg.lidarr.download_dir, folder_name)
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
            if (not os.path.isdir(self.staging_folder) or self.staging_folder not in source_dirs):
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

        success, source_dirs = self.move_album_files()
        if not success:
            if staging_folder_created:
                try:
                    os.rmdir(self.staging_folder)
                except OSError:
                    logger.warning(f"Could not remove temp import directory {self.staging_folder}")

            self.wanted_album.failure = "Download processing failed"
            return

        for source_dir in source_dirs:
            if source_dir == self.staging_folder:
                continue
            try:
                os.rmdir(source_dir)
            except OSError:
                logger.warning(f"Skipping removal of {source_dir} because it is not empty")

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

    def refresh_download_status(self, statuses: dict[tuple[str, str | int], dict]) -> bool:
        """Attach transfer statuses from a shared snapshot."""
        ok = True

        for file in self.files:
            status = statuses.get((file["username"], file["id"]))
            state = status.get("state") if isinstance(status, dict) else None
            file["status"] = status if isinstance(state, str) and state else None

            if file["status"] is None and file.get("required", True):
                logger.warning(f"Missing download status for {file['filename']}")
                ok = False

        return ok

    def discard_incomplete_optional_files(self) -> None:
        """Cancel optional transfers that did not complete before required files finished."""
        optional_files = [
            file for file in self.files
            if not file.get("required", True)
            and (file.get("status") or {}).get("state") != "Completed, Succeeded"
        ]
        if optional_files:
            self.wanted_album.cancel_and_delete(optional_files)
            self.files = [file for file in self.files if file not in optional_files]

    def cancel_and_delete(self) -> None:
        """Cancel this album's downloads and remove their local files."""
        self.wanted_album.cancel_and_delete(self.files)

    def fail(self, reason: str) -> bool:
        """Cancel this album, record its failure, and mark it terminal."""
        self.cancel_and_delete()
        self.wanted_album.failure = reason
        logger.info(f"{reason}: {self.artist} - {self.title}")
        return True

    def try_next_candidate(self, reason: str) -> bool:
        """Replace the failed album attempt, returning True when no candidate remains."""
        failed_candidate = self.candidate
        failed_location = "unknown source"
        if failed_candidate is not None:
            failed_location = ", ".join(
                f"{source.username}\\{source.file_dir}"
                for source in failed_candidate.sources
            )

        self.wanted_album.cancel_and_delete(self.files, remove=True)

        while self.fallback_candidates:
            candidate = self.fallback_candidates.popleft()
            downloads = self.wanted_album.enqueue_candidate(candidate)
            if not downloads:
                logger.info(
                    f"Stored candidate unavailable for {self.artist} - {self.title}; "
                    f"continuing to the next candidate"
                )
                continue

            self.files = downloads
            self.candidate = candidate
            self.count_start = time.monotonic()
            self.import_folder = None
            self.staging_folder = None
            self.wanted_album.failure = None
            self.log_replacement(failed_location, candidate, reason)
            return False

        self.files = []
        self.candidate = None
        self.fallback_candidates.clear()
        self.wanted_album.failure = reason
        logger.info(f"{reason}: {self.artist} - {self.title}")
        return True

    def log_replacement(
        self,
        failed_location: str,
        candidate: DownloadCandidate,
        reason: str,
    ) -> None:
        replacement_location = ", ".join(
            f"{source.username}\\{source.file_dir}"
            for source in candidate.sources
        )
        logger.info(
            f"Replaced failed candidate ({failed_location}) for {self.artist} - "
            f"{self.title} after {reason} with {replacement_location}; "
            f"score={candidate.match_score:.3f}, "
            f"free-slot={candidate.has_free_upload_slot}, "
            f"upload-speed={candidate.upload_speed:g}, "
            f"queue-length={candidate.queue_length}"
        )

    def poll(self, statuses: dict[tuple[str, int], dict]) -> bool:
        """Interpret one shared transfer snapshot, returning True when terminal."""
        elapsed = time.monotonic() - self.count_start

        if not self.refresh_download_status(statuses):
            if elapsed >= self.cfg.slskd.stalled_timeout:
                if self.cfg.slskd.requeue_failed_downloads:
                    return self.try_next_candidate("Timeout waiting for download status")
                return self.fail("Timeout waiting for download status")
            return False

        required_files = self.required_files
        states = [self.file_state(file) for file in required_files]
        problems = [file for file in required_files if self.file_state(file) in self.ERRORS]

        if all(state == self.SUCCESS for state in states):
            logger.info(f"Completed download of {self.artist} - {self.title}")
            self.discard_incomplete_optional_files()
            self.candidate = None
            self.fallback_candidates.clear()
            self.process_completed_album()
            return True

        if elapsed >= self.cfg.slskd.stalled_timeout:
            if self.cfg.slskd.requeue_failed_downloads:
                return self.try_next_candidate("Timeout waiting for download")
            return self.fail("Timeout waiting for download")

        if (states.count(self.REMOTE) == len(required_files) and elapsed >= self.cfg.slskd.remote_queue_timeout):
            if self.cfg.slskd.requeue_failed_downloads:
                return self.try_next_candidate("Remote queue timeout")
            return self.fail("Remote queue timeout")

        if problems:
            if self.cfg.slskd.requeue_failed_downloads:
                return self.try_next_candidate("Failed grab")
            return self.fail("Failed grab")

        return False

    def move_failed_import(self, src_path) -> str | None:
        """Move a failed Lidarr import's folder into `cfg.slskd.failed_imports_dir`, avoiding name clashes."""
        failed_imports_dir = self.cfg.slskd.failed_imports_dir

        os.makedirs(failed_imports_dir, exist_ok=True)

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
        """Trigger a Lidarr scan and record its final result."""
        command = lidarr.post_command(
            name="DownloadedAlbumsScan",
            path=self.import_folder,
        )
        logger.info(f"Starting Lidarr import for: {self.title} ID: {command['id']}")

        deadline = time.monotonic() + self.cfg.lidarr.import_timeout
        while True:
            current_task = lidarr.get_command(command["id"])

            if not isinstance(current_task, dict):
                logger.error(f"Invalid Lidarr command response: {current_task!r}")
                self.wanted_album.failure = "Invalid Lidarr import response"
                return

            if current_task.get("status") in ("completed", "failed"):
                break
            if time.monotonic() >= deadline:
                logger.error(f"Timed out waiting for Lidarr import of {self.artist} - {self.title}")
                self.wanted_album.state.import_pending = True
                return
            time.sleep(2)

        path = current_task.get("body", {}).get("path") or self.import_folder
        logger.info(f"{current_task.get('commandName', 'Lidarr command')} {current_task.get('message', '')} from: {path}")
        failed = (current_task.get("status") == "failed" or current_task.get("result") == "unsuccessful")
        if not failed:
            self.wanted_album.state.import_failed = False
            return

        self.wanted_album.failure = "Lidarr import failed"
        try:
            self.move_failed_import(path)
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

    def refresh_download_status(self) -> dict[tuple[str, int], dict]:
        """Fetch each active user's downloads and index them by username and ID."""
        statuses = {}
        usernames = {
            file["username"]
            for album in self.albums
            for file in album.files
        }

        for username in usernames:
            try:
                downloads = slskd.transfers.get_downloads(username=username)
                for directory in downloads["directories"]:
                    for file in directory["files"]:
                        if "id" in file:
                            statuses[(username, file["id"])] = file
            except Exception:
                logger.exception(f"Error getting downloads for {username}")

        return statuses

    def monitor_downloads(self) -> None:
        """Poll active albums until every album reaches a terminal state."""
        while self.albums:
            statuses = self.refresh_download_status()
            for album in self.albums.copy():
                if album.poll(statuses):
                    self.albums.remove(album)
                    break
            else:
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


def verify_filetype(file, allowed_filetype):
    """Return whether a search-result file matches an allowed filetype and quality."""
    extension, _, attributes = allowed_filetype.partition(" ")
    current_extension = file["filename"].rsplit(".", 1)[-1]

    if current_extension.lower() != extension.lower():
        return False
    if not attributes:
        return True

    if "/" not in attributes:
        bitrate = file.get("bitRate")
        return bool(bitrate) and str(bitrate) == attributes

    bitdepth, samplerate = attributes.split("/", 1)
    try:
        samplerate = str(int(float(samplerate) * 1000))
    except ValueError:
        logger.warning("Invalid samplerate in allowed filetype")
        return False

    file_bitdepth = file.get("bitDepth")
    file_samplerate = file.get("sampleRate")
    if not file_bitdepth or not file_samplerate:
        return False

    return str(file_bitdepth) == bitdepth and str(file_samplerate) == samplerate


def slskd_do_enqueue(username, files, file_dir):
    """Enqueue files for download from a user and return the ones slskd accepted.

    Each returned file dict is annotated with the tracking details (id, file_dir, username,
    size) needed to poll its download status later.
    """
    try:
        before = slskd.transfers.get_downloads(username=username)
    except HTTPError as ex:
        if ex.response is None or ex.response.status_code != 404:
            logger.warning(f"Failed to snapshot existing downloads for {username} before enqueue", exc_info=True)
            return None
        before = {"directories": []}
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
    if isinstance(enqueue, dict):
        enqueue = enqueue.get("files", [enqueue])
    if not isinstance(enqueue, list):
        enqueue = []

    accepted_ids = {
        record["filename"]: record["id"]
        for record in enqueue
        if isinstance(record, dict) and "filename" in record and "id" in record
    }
    required_names = {file["filename"] for file in files if file.get("required", True)}
    deadline = time.monotonic() + 30 # add additional 30 seconds to wait for slskd to update the download status

    while not required_names.issubset(accepted_ids) and time.monotonic() < deadline:
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
        if not required_names.issubset(accepted_ids):
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


def acquire_lock_file(path: str):
    """Acquire an OS lock that is released automatically if the process crashes."""
    lock_file = open(path, "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        logger.info("Soulseekarr instance is already running.")
        return None
    return lock_file


def setup_logging(config: dict, var_dir: str) -> None:
    """Configure console and optional rotating-file logging."""
    from logging.handlers import RotatingFileHandler

    log_config = DEFAULT_LOGGING | (config.get("logging") or {})
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout)
    ]
    log_file_path = None

    if log_config["log_to_file"]:
        log_file_path = os.path.join(
            var_dir,
            log_config["log_file"],
        )
        handlers.append(
            RotatingFileHandler(
                log_file_path,
                maxBytes=int(log_config["max_bytes"]),
                backupCount=int(log_config["backup_count"]),
            )
        )

    logging.basicConfig(
        level=log_config["level"],
        format=log_config["format"],
        datefmt=log_config["datefmt"],
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if log_file_path:
        logger.info(f"Logging to file: {log_file_path}")


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
                albums_to_append = WantedAlbums.get_wanted(config)
                if albums_to_append:
                    logger.info(f"Fetched {len(albums_to_append)} albums from '{source}' list that aren't on the deny list and/or blacklisted.")
                    wanted_albums.extend(albums_to_append)
                else:
                    logger.info(f"No albums fetched from '{source}' list that aren't on the deny list and/or blacklisted.")

            wanted_albums = wanted_albums.deduplicate()

            if not wanted_albums:
                logger.info("No wanted albums available.")
                if interval <= 0:
                    break
                time.sleep(interval)
                continue

            logger.info(f"Total wanted albums to process: {len(wanted_albums)}")

            if cfg.lidarr.shuffle_all:
                logger.info("shuffle_all: True (shuffling all wanted albums before processing)")
                wanted_albums.shuffle()

            try:
                total_failed = wanted_albums.grab_most_wanted(
                    monitor_downloads=cfg.slskd.monitor_downloads,
                )
                if total_failed == 0:
                    logger.info("Soulseekarr finished.")
                else:
                    logger.info(f"{total_failed}: releases failed to find a match in the search results and are still wanted.")
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


if __name__ == "__main__":
    sys.exit(main())
