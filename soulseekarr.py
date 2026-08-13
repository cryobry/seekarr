#!/usr/bin/env python

import argparse
import configparser
import math
import re
import os
import sys
import time
import shutil
import difflib
import logging
import json
from datetime import datetime
import copy
from dataclasses import dataclass
import music_tag
import slskd_api
import yaml
from requests.exceptions import HTTPError, RequestException
from pyarr import LidarrAPI
from pyarr.exceptions import PyarrError


def expand_env_vars(value):
    """Recursively expand $VAR/${VAR} references in strings loaded from the YAML config."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def as_list(value, lower: bool = False) -> list[str]:
    """Normalize a YAML list config value into stripped, non-empty items."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]  # Treat a bare scalar as a single-item list instead of splitting it into characters.
    items = [str(item).strip() for item in value if str(item).strip()]
    return [item.lower() for item in items] if lower else items


def env_override(section: str, key: str, value):
    """Override a resolved config value with an env var named "<SECTION>_<KEY>".

    Matches the YAML path (e.g. lidarr.api_key -> LIDARR_API_KEY), coercing the env var to
    match the existing value's type.
    """
    raw = os.environ.get(f"{section}_{key}".upper())
    if raw is None:
        return value
    if isinstance(value, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, list):
        return raw.split(",")
    if isinstance(value, int):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(value, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def require_config_value(value, name: str):
    """Raise a clear error if a required config value is missing from both config.yml and its env var."""
    if value is None or value == "":
        raise ValueError(f"Missing required config value: {name} (set it in config.yml or via its env var)")
    return value


# Allows backwards compatibility for users updating an older version of Soulseekarr
# without using the new "logging" section in the config.yml file.
DEFAULT_LOGGING_CONF = {
    "level": "INFO",
    "format": "[%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s: %(message)s",
    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
}


@dataclass
class LidarrConfig:
    api_key: str
    host_url: str
    download_dir: str
    disable_sync: bool
    sources: list[str] # missing, cutoff_unmet
    type: str
    page_size: int
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
    use_extension_whitelist: bool
    extensions_whitelist: list[str]
    rename_download_folders: bool
    allowed_filetypes: list[str]


@dataclass
class AppConfig:
    lidarr: LidarrConfig
    slskd: SlskdConfig
    # Paths
    lock_file_path: str
    config_file_path: str
    current_page_file_path: str
    failed_import_denylist_file_path: str

    @classmethod
    def from_yaml(cls, data: dict, args, source: str | None = None) -> "AppConfig":
        """Build an AppConfig from parsed config.yml data, env var overrides, and CLI args.

        Layers this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the
        top-level `lidarr`/`slskd` defaults, then applies per-key env var overrides.
        """
        lidarr_cfg: dict = data.get("lidarr") or {}
        slskd_cfg: dict = data.get("slskd") or {}

        # Layer this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the
        # top-level lidarr/slskd defaults. Called once per source in the run_once loop so the
        # global cfg reflects the source currently being processed.
        source_cfg: dict = (data.get(source) or {}) if source else {}
        resolved_lidarr = {**lidarr_cfg, **(source_cfg.get("lidarr") or {})}
        resolved_slskd = {**slskd_cfg, **(source_cfg.get("slskd") or {})}

        lidarr = LidarrConfig(
            api_key=require_config_value(env_override("lidarr", "api_key", lidarr_cfg.get("api_key")), "lidarr.api_key"),
            host_url=require_config_value(env_override("lidarr", "host_url", lidarr_cfg.get("host_url")), "lidarr.host_url"),
            download_dir=require_config_value(env_override("lidarr", "download_dir", lidarr_cfg.get("download_dir")), "lidarr.download_dir"),
            disable_sync=bool(env_override("lidarr", "disable_sync", resolved_lidarr.get("disable_sync", False))),
            sources=as_list(env_override("lidarr", "sources", lidarr_cfg.get("sources", ["missing"])), lower=True),
            type=str(env_override("lidarr", "type", resolved_lidarr.get("type", "first_page"))).lower().strip(),
            page_size=int(env_override("lidarr", "page_size", resolved_lidarr.get("page_size", 10))),
            title_blacklist=as_list(env_override("lidarr", "title_blacklist", resolved_lidarr.get("title_blacklist")), lower=True),
            failed_import_denylist=bool(env_override("lidarr", "failed_import_denylist", resolved_lidarr.get("failed_import_denylist", True))),
            use_selected_lidarr_release=bool(env_override("lidarr", "use_selected_lidarr_release", resolved_lidarr.get("use_selected_lidarr_release", False))),
            use_most_common_tracknum=bool(env_override("lidarr", "use_most_common_tracknum", resolved_lidarr.get("use_most_common_tracknum", True))),
            allow_multi_disc=bool(env_override("lidarr", "allow_multi_disc", resolved_lidarr.get("allow_multi_disc", True))),
            accepted_countries=as_list(
                env_override(
                    "lidarr",
                    "accepted_countries",
                    resolved_lidarr.get("accepted_countries", ["Europe", "Japan", "United Kingdom", "United States", "[Worldwide]", "Australia", "Canada"]),
                )
            ),
            skip_region_check=bool(env_override("lidarr", "skip_region_check", resolved_lidarr.get("skip_region_check", False))),
            accepted_formats=as_list(env_override("lidarr", "accepted_formats", resolved_lidarr.get("accepted_formats", ["CD", "Digital Media", "Vinyl"]))),
        )

        slskd = SlskdConfig(
            api_key=require_config_value(env_override("slskd", "api_key", slskd_cfg.get("api_key")), "slskd.api_key"),
            host_url=require_config_value(env_override("slskd", "host_url", slskd_cfg.get("host_url")), "slskd.host_url"),
            download_dir=require_config_value(env_override("slskd", "download_dir", slskd_cfg.get("download_dir")), "slskd.download_dir"),
            url_base=str(env_override("slskd", "url_base", slskd_cfg.get("url_base", "/"))),
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
            ignored_users=as_list(env_override("slskd", "ignored_users", resolved_slskd.get("ignored_users"))),
            search_blacklist=as_list(env_override("slskd", "search_blacklist", resolved_slskd.get("search_blacklist"))),
            album_prepend_artist=bool(env_override("slskd", "album_prepend_artist", resolved_slskd.get("album_prepend_artist", False))),
            filtering=bool(env_override("slskd", "filtering", resolved_slskd.get("filtering", False))),
            use_extension_whitelist=bool(env_override("slskd", "use_extension_whitelist", resolved_slskd.get("use_extension_whitelist", False))),
            extensions_whitelist=as_list(env_override("slskd", "extensions_whitelist", resolved_slskd.get("extensions_whitelist", ["txt", "nfo", "jpg"]))),
            rename_download_folders=bool(env_override("slskd", "rename_download_folders", resolved_slskd.get("rename_download_folders", True))),
            allowed_filetypes=as_list(env_override("slskd", "allowed_filetypes", resolved_slskd.get("allowed_filetypes", ["flac", "mp3"]))),
        )

        return cls(
            lidarr=lidarr,
            slskd=slskd,
            lock_file_path=os.path.join(args.var_dir, ".soulseekarr.lock"),
            config_file_path=os.path.join(args.config_dir, "config.yml"),
            current_page_file_path=os.path.join(args.var_dir, ".current_page.txt"),
            failed_import_denylist_file_path=os.path.join(args.var_dir, "failed_imports.json"),
        )

# ===== API Clients & Logging =====
lidarr: LidarrAPI = None  # type: ignore[assignment]
slskd: slskd_api.SlskdClient = None  # type: ignore[assignment]
logger = logging.getLogger("soulseekarr")
cfg: AppConfig = None  # type: ignore[assignment]

# ===== Runtime State & Caches =====
search_cache: dict = {}
folder_cache: dict = {}
broken_user: list = []
# Albums grabbed while Lidarr sync is disabled, so we don't regrab them on later loops
# in the same run (Lidarr never learns about them, so it can't tell us itself). Not
# persisted across restarts.
grabbed_albums: set = set()


def album_match(lidarr_tracks, slskd_tracks, username, filetype, album_name):
    """Check whether a user's Soulseek files are a full-album match for the given Lidarr tracks.

    Compares each Lidarr track title against every candidate filename with fuzzy string
    matching (plus several filename-cleanup heuristics), requiring every track to clear
    `cfg.slskd.minimum_match_ratio` for the album to count as matched.
    """
    counted = []
    total_match = 0.0
    filetype_ext = filetype.split(" ")[0]

    for lidarr_track in lidarr_tracks:
        lidarr_filename = lidarr_track["title"] + "." + filetype_ext
        best_match = 0.0

        for slskd_track in slskd_tracks:
            slskd_filename = slskd_track["filename"]

            # Try to match the ratio with the exact filenames
            ratio = difflib.SequenceMatcher(None, lidarr_filename, slskd_filename).ratio()

            # If ratio is a bad match try and split off (with " " as the separator) the garbage at the start of the slskd_filename and try again
            ratio = check_ratio(" ", ratio, lidarr_filename, slskd_filename)
            # Same but with "_" as the separator
            ratio = check_ratio("_", ratio, lidarr_filename, slskd_filename)

            # Same checks but prepend album name.
            ratio = check_ratio("", ratio, album_name + " " + lidarr_filename, slskd_filename)
            ratio = check_ratio(" ", ratio, album_name + " " + lidarr_filename, slskd_filename)
            ratio = check_ratio("_", ratio, album_name + " " + lidarr_filename, slskd_filename)

            if ratio > best_match:
                best_match = ratio
                if best_match == 1.0:  # Can't do better than a perfect match
                    break

        if best_match > cfg.slskd.minimum_match_ratio:
            counted.append(lidarr_filename)
            total_match += best_match

    if not counted:
        return False

    if len(counted) == len(lidarr_tracks) and username not in cfg.slskd.ignored_users:
        logger.info(f"Found match from user: {username} for {len(counted)} tracks! Track attributes: {filetype}")
        logger.info(f"Average sequence match ratio: {total_match / len(counted)}")
        logger.info("SUCCESSFUL MATCH")
        logger.info("-------------------")
        return True

    return False


def check_ratio(separator, ratio, lidarr_filename, slskd_filename):
    if ratio < cfg.slskd.minimum_match_ratio:
        if separator != "":
            lidarr_filename_word_count = len(lidarr_filename.split()) * -1
            truncated_slskd_filename = " ".join(slskd_filename.split(separator)[lidarr_filename_word_count:])
            ratio = difflib.SequenceMatcher(None, lidarr_filename, truncated_slskd_filename).ratio()
        else:
            ratio = difflib.SequenceMatcher(None, lidarr_filename, slskd_filename).ratio()

        return ratio
    return ratio


def album_track_num(directory):
    files = directory["files"]
    allowed_filetypes_no_attributes = [item.split(" ")[0] for item in cfg.slskd.allowed_filetypes]
    count = 0
    index = -1
    filetype = ""
    for file in files:
        if file["filename"].split(".")[-1] in allowed_filetypes_no_attributes:
            new_index = allowed_filetypes_no_attributes.index(file["filename"].split(".")[-1])

            if index == -1:
                index = new_index
                filetype = allowed_filetypes_no_attributes[index]
            elif new_index != index:
                filetype = ""
                break

            count += 1

    return_data = {"count": count, "filetype": filetype}
    return return_data


def sanitize_folder_name(folder_name):
    valid_characters = re.sub(r'[<>:."/\\|?*]', "", folder_name)
    return valid_characters.strip()


def cancel_and_delete(files):
    """Cancel each file's in-progress slskd download and remove its local download folder."""
    for file in files:
        try:
            slskd.transfers.cancel_download(username=file["username"], id=file["id"])
        except Exception:
            logger.warning(f"Failed to cancel download {file['filename']} for {file['username']}", exc_info=True)
        delete_dir = os.path.join(cfg.slskd.download_dir, file["file_dir"].split("\\")[-1])

        if os.path.exists(delete_dir):
            shutil.rmtree(delete_dir)


def release_trackcount_mode(releases):
    track_count = {}

    for release in releases:
        trackcount = release["trackCount"]
        if trackcount in track_count:
            track_count[trackcount] += 1
        else:
            track_count[trackcount] = 1

    most_common_trackcount = None
    max_count = 0

    for trackcount, count in track_count.items():
        if count > max_count:
            max_count = count
            most_common_trackcount = trackcount

    return most_common_trackcount


def choose_release(artist_name, releases):
    """Pick the best release to search for from an album's list of Lidarr releases.

    Prefers the release manually selected in Lidarr (if `use_selected_lidarr_release`), then
    the first release matching the accepted countries/formats/status/track-count settings,
    falling back to the most common track count or simply the first release otherwise.
    """
    if cfg.lidarr.use_selected_lidarr_release:
        for release in releases:
            if release.get("monitored"):
                logger.info(f"Using selected Lidarr release for {artist_name}: {release['format']}, {release['trackCount']} tracks, ID: {release['id']}")
                return release

    most_common_trackcount = release_trackcount_mode(releases)

    for release in releases:
        country = release["country"][0] if release["country"] else None

        if release["format"][1] == "x" and cfg.lidarr.allow_multi_disc:
            format_accepted = release["format"].split("x", 1)[1] in cfg.lidarr.accepted_formats
        else:
            format_accepted = release["format"] in cfg.lidarr.accepted_formats

        if cfg.lidarr.use_most_common_tracknum:
            if release["trackCount"] == most_common_trackcount:
                track_count_bool = True
            else:
                track_count_bool = False
        else:
            track_count_bool = True

        if (cfg.lidarr.skip_region_check or country in cfg.lidarr.accepted_countries) and format_accepted and release["status"] == "Official" and track_count_bool:
            logger.info(
                ", ".join(
                    [
                        f"Selected release for {artist_name}: {release['status']}",
                        str(country),
                        release["format"],
                        f"Mediums: {release['mediumCount']}",
                        f"Tracks: {release['trackCount']}",
                        f"ID: {release['id']}",
                    ]
                )
            )

            return release

    if cfg.lidarr.use_most_common_tracknum:
        for release in releases:
            if release["trackCount"] == most_common_trackcount:
                return release

    return releases[0]


def verify_filetype(file, allowed_filetype):
    """Check whether a slskd search result file matches an `allowed_filetypes` config entry.

    Matches on file extension, and if the config entry also specifies quality attributes
    (bitrate, or bitdepth/samplerate), verifies those against the file's metadata too.
    """
    current_filetype = file["filename"].split(".")[-1]
    bitdepth = None
    samplerate = None
    bitrate = None

    if "bitRate" in file:
        bitrate = file["bitRate"]
    if "sampleRate" in file:
        samplerate = file["sampleRate"]
    if "bitDepth" in file:
        bitdepth = file["bitDepth"]

    # Check if the types match up for the current files type and the current type from the config
    if current_filetype == allowed_filetype.split(" ")[0]:
        # Check if the current type from the config specifies other attributes than the filetype (bitrate etc)
        if " " in allowed_filetype:
            selected_attributes = allowed_filetype.split(" ")[1]
            # If it is a bitdepth/samplerate pair instead of a simple bitrate
            if "/" in selected_attributes:
                selected_bitdepth = selected_attributes.split("/")[0]
                try:
                    selected_samplerate = str(int(float(selected_attributes.split("/")[1]) * 1000))
                except (ValueError, IndexError):
                    logger.warning("Invalid samplerate in selected_attributes")
                    return False

                if bitdepth and samplerate:
                    if str(bitdepth) == str(selected_bitdepth) and str(samplerate) == str(selected_samplerate):
                        return True
                else:
                    return False
            # If it is a bitrate
            else:
                selected_bitrate = selected_attributes
                if bitrate:
                    if str(bitrate) == str(selected_bitrate):
                        return True
                else:
                    return False
        # If no bitrate or other info then it is a match so return true
        else:
            return True
    else:
        return False


def download_filter(allowed_filetype, directory):
    """Filter a slskd directory listing down to the allowed filetype (and whitelist, if enabled).

    This prevents downloading m3u/cue/txt/jpg/etc. files that are sometimes stored alongside
    the music files in the same folder.
    """
    logging.debug("download_filtering")
    if cfg.slskd.filtering:
        whitelist = []  # Init an empty list to take just the allowed_filetype
        if cfg.slskd.use_extension_whitelist:
            whitelist = copy.deepcopy(cfg.slskd.extensions_whitelist)  # Copy the whitelist to allow us to append the allowed_filetype
        whitelist.append(allowed_filetype.split(" ")[0])
        unwanted = []
        logger.debug(f"Accepted extensions: {whitelist}")
        for file in directory["files"]:
            for extension in whitelist:
                if file["filename"].split(".")[-1].lower() == extension.lower():
                    break  # Jump out and don't add wanted files to the unwanted list
            else:
                unwanted.append(file["filename"])  # Add to list of files to remove from the wanted list
                logger.debug(f"Unwanted file: {file['filename']}")
        if len(unwanted) > 0:
            temp = []
            logger.debug(f"Unwanted Files: {unwanted}")
            for file in directory["files"]:
                if file["filename"] not in unwanted:
                    logger.debug(f"Added file to queue: {file['filename']}")
                    temp.append(file)  # Build the new list of files
            directory["files"] = temp
            for files in temp:
                logger.debug(f"File in final list: {files['filename']}")
            return directory  # Return the modified list
    return directory  # If we didn't find unwanted files or we aren't filtering just return the original list


def check_for_match(tracks, allowed_filetype, file_dirs, username, album_name):
    """Fetch (and cache) a user's file listing for each candidate folder and check for an album match."""
    if username in broken_user:
        return False, {}, ""
    for file_dir in file_dirs:
        if username not in folder_cache:
            logger.debug(f"Add user to cache: {username}")
            folder_cache[username] = {}

        if file_dir not in folder_cache[username]:
            logger.info(f"User: {username} Folder: {file_dir} not in cache. Fetching from SLSKD")

            try:
                # Assumes slskd > 0.22.2, whose users.directory() returns a list of directories.
                directory = slskd.users.directory(username=username, directory=file_dir)[0]
            except HTTPError as ex:
                status_code = ex.response.status_code if ex.response is not None else "unknown"
                logger.warning(f'HTTP error getting directory from user "{username}" folder "{file_dir}": {status_code}')
                if ex.response is not None and ex.response.text:
                    logger.debug(f"SLSKD response body: {ex.response.text[:500]}")

                # slskd 5xx errors are generally transient server failures.
                if isinstance(status_code, int) and 500 <= status_code < 600:
                    continue

                broken_user.append(username)
                logger.debug(f"Updated broken users {broken_user}")
                return False, {}, ""
            except IndexError:
                logger.warning(f'Empty directory response from user "{username}" for folder "{file_dir}"')
                directory = {"files": []}
            except RequestException:
                logger.exception(f'Network error getting directory from user: "{username}"')
                return False, {}, ""
            except Exception:
                logger.exception(f'Error getting directory from user: "{username}"')
                return False, {}, ""
            folder_cache[username][file_dir] = copy.deepcopy(directory)
        else:
            logger.info(f"User: {username} Folder: {file_dir} in cache. Using cached value")
            directory = copy.deepcopy(folder_cache[username][file_dir])

        track_num = len(tracks)
        tracks_info = album_track_num(directory)

        if tracks_info["count"] == track_num and tracks_info["filetype"] != "":
            if album_match(tracks, directory["files"], username, allowed_filetype, album_name):
                return True, directory, file_dir
            else:
                continue
    return False, {}, ""


def is_blacklisted(title: str) -> bool:
    for word in cfg.lidarr.title_blacklist:
        if word != "" and word in title.lower():
            logger.info(f"Skipping {title} due to blacklisted word: {word}")
            return True
    return False


def filter_list(albums):
    """Apply the failed-import denylist, disable_sync grabbed-albums, and title blacklist filters.

    Combines what used to be several separate filtering passes into one pass for clarity.
    """
    temp_list = copy.deepcopy(albums)

    if cfg.lidarr.failed_import_denylist:
        import_denylist = load_failed_import_denylist(cfg.failed_import_denylist_file_path)
        filtered_temp = []
        for album in temp_list:
            if str(album["id"]) in import_denylist:
                logger.info(f"Skipping failed import album: {album['artist']['artistName']} - {album['title']} (ID: {album['id']})")
            else:
                filtered_temp.append(album)
        temp_list = filtered_temp

    if cfg.lidarr.disable_sync:
        # Lidarr never learns about these downloads, so it'll keep reporting them as wanted forever.
        # Track grabs in memory ourselves so we don't redownload the same album on every loop.
        filtered_temp = []
        for album in temp_list:
            if album["id"] in grabbed_albums:
                logger.info(f"Skipping already grabbed album: {album['artist']['artistName']} - {album['title']} (ID: {album['id']})")
            else:
                filtered_temp.append(album)
        temp_list = filtered_temp

    list_to_download = []
    for album in temp_list:
        if is_blacklisted(album["title"]):
            logger.info(f"Skipping blacklisted album: {album['artist']['artistName']} - {album['title']} (ID: {album['id']}")
            continue
        else:
            list_to_download.append(album)

    if len(list_to_download) > 0:
        return list_to_download
    else:
        return None


def search_for_album(album):
    """Search slskd for an album, poll until the search completes, and cache matching results.

    Builds the search query (optionally prepending the artist name and stripping blacklisted
    words), waits for the slskd search to finish or time out, and populates `search_cache` with
    each result's files grouped by user and allowed filetype.
    """
    album_title = album["title"]
    artist_name = album["artist"]["artistName"]
    album_id = album["id"]
    if len(album_title) == 1:  # Need to add some code to wrangle specific artist names in here.. ;)
        query = artist_name + " " + album_title
    else:
        query = artist_name + " " + album_title if cfg.slskd.album_prepend_artist else album_title

    original_query = query
    for word in cfg.slskd.search_blacklist:
        if word:
            # Case-insensitive replacement
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            query = pattern.sub("", query)

    # Clean up double spaces
    query = " ".join(query.split())

    if query != original_query:
        logger.info(f"Filtered search query: '{original_query}' -> '{query}'")

    logger.info(f"Searching for album: {query}")
    try:
        search = slskd.searches.search_text(
            searchText=query,
            searchTimeout=max(1, int(cfg.slskd.timeout * 1000)),
            filterResponses=True,
            maximumPeerQueueLength=cfg.slskd.maximum_peer_queue,
            minimumPeerUploadSpeed=cfg.slskd.minimum_peer_upload_speed,
        )
    except Exception:
        logger.exception(f"Failed to perform search via SLSKD: {query}")
        return False

    def cleanup_search():
        """Best-effort delete so a timeout/error doesn't leave the search stuck in SLSKD forever."""
        if cfg.slskd.delete_searches:
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
            time.sleep(1)
            if (time.time() - start_time) > cfg.slskd.timeout:
                logger.error("Failed to perform search via SLSKD due to timeout on search results.")
                cleanup_search()
                return False

        search_results = slskd.searches.search_responses(search["id"])  # We use this API call twice. Let's just cache it locally.
        logger.info(f"Search returned {len(search_results)} results")
        cleanup_search()
    except Exception:
        logger.exception(f"Failed to perform search via SLSKD: {query}")
        cleanup_search()
        return False

    if not search_results:
        return False

    if album_id not in search_cache:
        search_cache[album_id] = {}  # This is so we can check for matches we missed or if a user goes offline during our download

    for result in search_results:  # Switching to cached version. One less API call
        username = result["username"]
        if username not in search_cache[album_id]:
            # If we don't currently have a cache for a user set one up
            search_cache[album_id][username] = {}
        logger.info(f"Caching and truncating results for user: {username}")
        init_files = result["files"]  # init_files short for initial files. Before truncating
        # Search the returned files and only cache files that are of the allowed_filetypes
        for file in init_files:
            file_dir = file["filename"].rsplit("\\", 1)[0]  # split dir/filenames on \
            for allowed_filetype in cfg.slskd.allowed_filetypes:
                if verify_filetype(file, allowed_filetype):  # Check the filename for an allowed type
                    if allowed_filetype not in search_cache[album_id][username]:
                        search_cache[album_id][username][allowed_filetype] = []  # Init the cache for this allowed filetype
                    if file_dir not in search_cache[album_id][username][allowed_filetype]:
                        search_cache[album_id][username][allowed_filetype].append(file_dir)
    return True


def slskd_do_enqueue(username, files, file_dir):
    """Enqueue files for download from a user and return the ones slskd accepted.

    Each returned file dict is annotated with the tracking details (id, file_dir, username,
    size) needed to poll its download status later.
    """
    downloads = []
    try:
        enqueue = slskd.transfers.enqueue(username=username, files=files)
    except Exception:
        logger.debug("Enqueue failed", exc_info=True)
        return None
    if enqueue:
        time.sleep(5)
        try:
            download_list = slskd.transfers.get_downloads(username=username)
        except Exception:
            logger.warning(f"Failed to get download status for {username} after enqueue", exc_info=True)
            return None
        for file in files:
            for directory in download_list["directories"]:
                if directory["directory"] == file_dir:
                    for slskd_file in directory["files"]:
                        if file["filename"] == slskd_file["filename"]:
                            file_details = {}
                            file_details["filename"] = file["filename"]
                            file_details["id"] = slskd_file["id"]
                            file_details["file_dir"] = file_dir
                            file_details["username"] = username
                            file_details["size"] = file["size"]
                            downloads.append(file_details)
        return downloads
    else:
        return None


def slskd_download_status(downloads):
    """Fetch and attach the current slskd transfer status to each file dict in `downloads`."""
    ok = True
    for file in downloads:
        try:
            status = slskd.transfers.get_download(file["username"], file["id"])
            file["status"] = status
        except Exception:
            logger.exception(f"Error getting download status of {file['filename']}")
            file["status"] = None
            ok = False
    return ok


def downloads_all_done(downloads):
    """Summarize an album's download progress from its files' current statuses.

    Returns (all_done, error_list, remote_queue_count), where error_list is None if there are
    no failed files.
    """
    all_done = True
    error_list = []
    remote_queue = 0
    for file in downloads:
        if file["status"] is not None:
            if not file["status"]["state"] == "Completed, Succeeded":
                all_done = False
            if file["status"]["state"] in [
                "Completed, Cancelled",
                "Completed, TimedOut",
                "Completed, Errored",
                "Completed, Rejected",
                "Completed, Aborted",
            ]:
                error_list.append(file)
            if file["status"]["state"] == "Queued, Remotely":
                remote_queue += 1
    if not len(error_list) > 0:
        error_list = None
    return all_done, error_list, remote_queue


def try_enqueue(all_tracks, results, allowed_filetype, artist_name, album_name):
    """Try to find and enqueue a single-disk album match from any user in `results`."""
    for username in results:
        if allowed_filetype not in results[username]:
            continue
        logger.debug(f"Parsing result from user: {username}")
        file_dirs = results[username][allowed_filetype]
        found, directory, file_dir = check_for_match(all_tracks, allowed_filetype, file_dirs, username, album_name)
        if found:
            directory = download_filter(allowed_filetype, directory)
            for i in range(0, len(directory["files"])):
                directory["files"][i]["filename"] = file_dir + "\\" + directory["files"][i]["filename"]
            try:
                downloads = slskd_do_enqueue(username=username, files=directory["files"], file_dir=file_dir)
                if downloads is not None:
                    return True, downloads
                else:
                    logger.info(f"Failed to enqueue download to slskd for {artist_name} - {album_name} from {username}")
            except Exception as e:
                logger.warning(f"Exception enqueueing tracks: {e}")
                logger.info(f"Exception enqueueing download to slskd for {artist_name} - {album_name} from {username}")
    logger.info(f"Failed to enqueue {artist_name} - {album_name}")
    return False, None


def try_multi_enqueue(release, all_tracks, results, allowed_filetype, artist_name, album_name):
    """Try to find and enqueue a multi-disk album match, sourcing each disk independently.

    Requires every disk in the release to be matched (by any user) before enqueueing; if any
    disk can't be sourced, the whole attempt fails with nothing downloaded.
    """
    split_release = []
    for media in release["media"]:
        disk = {}
        disk["source"] = None
        disk["tracks"] = []
        disk["disk_no"] = media["mediumNumber"]
        disk["disk_count"] = len(release["media"])
        for track in all_tracks:
            if track["mediumNumber"] == media["mediumNumber"]:
                disk["tracks"].append(track)
        split_release.append(disk)
    total = len(split_release)
    count_found = 0
    for disk in split_release:
        for username in results:
            if allowed_filetype not in results[username]:
                continue
            file_dirs = results[username][allowed_filetype]
            found, directory, file_dir = check_for_match(disk["tracks"], allowed_filetype, file_dirs, username, album_name)
            if found:
                directory = download_filter(allowed_filetype, directory)
                disk["source"] = (username, directory, file_dir)
                count_found += 1
                break
        else:
            return (
                False,
                None,
            )  # Only runs if we complete the loop without finding a source for the current disk regardless of how many other disks we located. All or nothing.
    if count_found == total:
        all_downloads = []
        enqueued = 0
        for disk in split_release:
            username, directory, file_dir = disk["source"]
            for i in range(0, len(directory["files"])):
                directory["files"][i]["filename"] = file_dir + "\\" + directory["files"][i]["filename"]
            try:
                downloads = slskd_do_enqueue(username=username, files=directory["files"], file_dir=file_dir)
                if downloads is not None:
                    for file in downloads:
                        file["disk_no"] = disk["disk_no"]
                        file["disk_count"] = disk["disk_count"]
                    all_downloads.extend(downloads)
                    enqueued += 1
                else:
                    logger.info(f"Failed to enqueue download to slskd for {artist_name} - {album_name} from {username}")
                    # Delete ALL other downloads in all_downloads list
                    if len(all_downloads) > 0:
                        cancel_and_delete(all_downloads)
                        return False, None
            except Exception:
                logger.exception("Exception enqueueing tracks")
                logger.info(f"Exception enqueueing download to slskd for {artist_name} - {album_name} from {username}")
                # Delete all other downloads in all_downloads list
                if len(all_downloads) > 0:
                    cancel_and_delete(all_downloads)
                    return False, None
        if enqueued == total:
            return True, all_downloads
        else:
            # Delete all other downloads
            if len(all_downloads) > 0:
                cancel_and_delete(all_downloads)
            return False, None

    else:
        return False, None


def find_download(album, grab_list):
    """Find a download source for `album` by trying every allowed filetype and release.

    For each quality, tries the single-disk match path first, falling back to the multi-disk
    path for multi-media releases. Populates `grab_list[album_id]` and returns True on success.
    """
    album_id = album["id"]
    artist_name = album["artist"]["artistName"]
    album_name = album["title"]
    artist_id = album["artistId"]
    results = search_cache[album_id]

    # Releases/tracks don't change per quality, so fetch them once and reuse across the
    # allowed_filetypes loop below instead of hitting the Lidarr API for every quality.
    releases = lidarr.get_album(album_id)["releases"]
    tracks_by_release: dict = {}

    for allowed_filetype in cfg.slskd.allowed_filetypes:
        if not any(allowed_filetype in results[username] for username in results):
            logger.debug(f"No search results for Quality: {allowed_filetype}. Skipping.")
            continue

        logger.info(f"Checking for Quality: {allowed_filetype}")
        remaining_releases = list(releases)
        for _ in range(0, len(remaining_releases)):
            if len(remaining_releases) == 0:
                break
            release = choose_release(artist_name, remaining_releases)
            remaining_releases.remove(release)
            release_id = release["id"]
            if release_id not in tracks_by_release:
                tracks_by_release[release_id] = lidarr.get_tracks(artistId=artist_id, albumId=album_id, albumReleaseId=release_id)
            all_tracks = tracks_by_release[release_id]
            found, downloads = try_enqueue(all_tracks, results, allowed_filetype, artist_name, album_name)

            if not found and len(release["media"]) > 1:
                found, downloads = try_multi_enqueue(release, all_tracks, results, allowed_filetype, artist_name, album_name)

            if found:
                grab_list[album_id] = {
                    "files": downloads,
                    "filetype": allowed_filetype,
                    "title": album_name,
                    "artist": artist_name,
                    "year": album["releaseDate"][0:4],
                }
                return True
    return False


def search_and_queue(albums):
    """Search and enqueue every album in `albums`, respecting `minimum_search_interval`.

    Returns (grab_list, failed_search, failed_grab): `grab_list` holds the enqueued downloads,
    `failed_search` holds albums with no slskd search results, and `failed_grab` holds albums
    that had results but no match/enqueue succeeded.
    """
    grab_list = {}
    failed_grab = []
    failed_search = []
    for i, album in enumerate(albums):
        search_start = time.time()
        if search_for_album(album):
            if not find_download(album, grab_list):
                failed_grab.append(album)
        else:
            failed_search.append(album)

        if cfg.slskd.minimum_search_interval > 0 and i < len(albums) - 1:
            elapsed = time.time() - search_start
            remaining = cfg.slskd.minimum_search_interval - elapsed
            if remaining > 0:
                logger.info(f"Search completed in {elapsed:.1f}s, waiting {remaining:.1f}s to meet minimum_search_interval")
                time.sleep(remaining)

    return grab_list, failed_search, failed_grab


def process_completed_album(album_data, failed_grab):
    """Move a fully-downloaded album into its import folder and trigger a Lidarr import.

    Renames/moves the downloaded files into a single folder, tags them, and asks Lidarr to
    scan it. Rolls back the moved files if anything fails partway through, and records the
    album in `failed_grab` (and the failed-import denylist) if the Lidarr import itself fails.
    If `disable_sync` is set, skips the Lidarr import entirely and just tracks the grab.
    """
    if cfg.slskd.rename_download_folders is True:
        import_folder_name = sanitize_folder_name(album_data["artist"] + " - " + album_data["title"] + " (" + album_data["year"] + ")")
    else:
        import_folder_name = album_data["files"][0]["file_dir"].rstrip("\\/").rsplit("\\", 1)[-1]
    import_folder_fullpath = os.path.join(cfg.slskd.download_dir, import_folder_name)
    lidarr_import_fullpath = os.path.join(cfg.lidarr.download_dir, import_folder_name)
    album_data["import_folder"] = lidarr_import_fullpath
    rm_dirs = []
    moved_files_history = []
    if not os.path.exists(import_folder_fullpath):
        os.mkdir(import_folder_fullpath)
    for file in album_data["files"]:
        file_folder = file["file_dir"].split("\\")[-1]
        filename = file["filename"].split("\\")[-1]
        src_folder = os.path.join(cfg.slskd.download_dir, file_folder)
        if src_folder not in rm_dirs:
            rm_dirs.append(src_folder)  # Multi disk albums are sometimes in multiple folders. eg. CD01 CD02. So we need to clean up both
        src_file = os.path.join(src_folder, filename)
        if "disk_no" in file and "disk_count" in file and file["disk_count"] > 1:
            filename = f"Disk {file['disk_no']} - {filename}"
        dst_file = os.path.join(import_folder_fullpath, filename)
        file["import_path"] = dst_file
        if os.path.abspath(src_file) == os.path.abspath(dst_file):
            continue
        try:
            shutil.move(src_file, dst_file)
            moved_files_history.append((src_file, dst_file))
        except Exception:
            logger.exception(f"Failed to move: {file['filename']} to temp location for import into Lidarr. Rolling back...")
            for src, dst in reversed(moved_files_history):
                try:
                    shutil.move(dst, src)
                except Exception:
                    logger.exception(f"Critical failure during rollback: could not move {dst} back to {src}")
            try:
                os.rmdir(import_folder_fullpath)
            except OSError:
                logger.warning(f"Could not remove temp import directory {import_folder_fullpath}")
            failed_grab.append(lidarr.get_album(album_data["album_id"]))
            return
    else:  # Only runs if all files are successfully moved
        for rm_dir in rm_dirs:
            if not rm_dir == import_folder_fullpath:
                try:
                    os.rmdir(rm_dir)
                except OSError:
                    logger.warning(f"Skipping removal of {rm_dir} because it's not empty.")
        if cfg.lidarr.disable_sync:
            logger.info(f"Sync disabled. Skipping Lidarr import of {album_data['artist']} - {album_data['title']}")
            grabbed_albums.add(album_data["album_id"])
            return
        logger.info(f"Attempting Lidarr import of {album_data['artist']} - {album_data['title']}")
        for file in album_data["files"]:
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
                song["albumartist"] = album_data["artist"]
                song["album"] = album_data["title"]
                song.save()
            except Exception:
                logger.exception(f"Error writing tags for: {file['import_path']}")
        command = lidarr.post_command(
            name="DownloadedAlbumsScan",
            path=album_data["import_folder"],
        )  # Album all tagged up and in a correctly named folder. This should work more reliably
        logger.info(f"Starting Lidarr import for: {album_data['title']} ID: {command['id']}")

        while True:
            current_task = lidarr.get_command(command["id"])
            if current_task["status"] == "completed" or current_task["status"] == "failed":
                break
            time.sleep(2)

        try:
            logger.info(f"{current_task['commandName']} {current_task['message']} from: {current_task['body']['path']}")

            if "Failed" in current_task["message"]:
                folder_path = move_failed_import(current_task["body"]["path"])
                failed_grab.append(lidarr.get_album(album_data["album_id"]))
                if cfg.lidarr.failed_import_denylist:
                    add_to_failed_import_denylist(
                        cfg.failed_import_denylist_file_path,
                        album_data["album_id"],
                        album_data["artist"],
                        album_data["title"],
                        folder_path,
                    )
        except Exception:
            logger.exception("Error printing lidarr task message")
            logger.error(current_task)


def monitor_downloads(grab_list, failed_grab):
    """Poll slskd until every album in `grab_list` finishes, errors out, or times out.

    Handles per-file hard errors (cancelled/timed out/errored/aborted) and rejections by
    requeuing individual files (up to a retry limit) before giving up on the whole album, and
    hands completed albums off to `process_completed_album`.
    """
    MAX_FILE_RETRIES = 4  # Max requeue attempts per file for hard errors (Errored, Cancelled, etc.)

    def delete_album(reason):
        cancel_and_delete(grab_list[album_id]["files"])
        logger.info(f"{reason} Album: {grab_list[album_id]['title']} Artist: {grab_list[album_id]['artist']}")
        del grab_list[album_id]
        failed_grab.append(lidarr.get_album(album_id))

    def requeue_file(album_id, file):
        """Requeue a single errored file. Returns True on success, False if enqueue failed."""
        data_dict = [{"filename": file["filename"], "size": file["size"]}]
        logger.info(f"Download error. Requeue file: {file['filename']}")
        requeue = slskd_do_enqueue(file["username"], data_dict, file["file_dir"])
        if requeue is not None:
            file["id"] = requeue[0]["id"]
            time.sleep(1)
            slskd_download_status(grab_list[album_id]["files"])
            return True
        return False

    def handle_hard_error(album_id, file, problems):
        """Handle Cancelled/TimedOut/Errored/Aborted files.

        Returns True if the album was deleted (caller should stop processing this album).
        """
        if len(problems) == len(grab_list[album_id]["files"]):
            delete_album("Failed grab of")
            return True
        if not cfg.slskd.requeue_failed_downloads:
            delete_album("Failed grab of")
            return True
        file.setdefault("retry", 0)
        file["retry"] += 1
        if file["retry"] > MAX_FILE_RETRIES:
            delete_album("Failed grab of")
            return True
        if not requeue_file(album_id, file):
            delete_album("Failed grab of")
            return True
        return False

    def handle_rejected(album_id, file, problems):
        """Handle Rejected files.

        Returns True if the album was deleted or a requeue was attempted (caller should stop
        processing this album this iteration). Rejected files often indicate grab limits; we
        wait for all other files to reach a stable state before requeuing.
        """
        files = grab_list[album_id]["files"]
        if len(problems) == len(files):
            delete_album("Failed grab of")
            return True
        if not cfg.slskd.requeue_failed_downloads:
            delete_album("Failed grab of")
            return True
        # Only requeue once all non-problem files have settled (no files mid-transfer).
        stable_states = ("Completed, Succeeded", "Queued, Remotely", "Queued, Locally")
        accounted = sum(1 for f in files if f["status"]["state"] in stable_states) + len(problems)
        if accounted < len(files):
            return False
        grab_list[album_id].setdefault("rejected_retries", 0)
        if grab_list[album_id]["rejected_retries"] >= int(len(files) * 1.2):
            delete_album("Failed grab of")
            return True
        if not requeue_file(album_id, file):
            delete_album("Failed grab of")
            return True
        grab_list[album_id]["rejected_retries"] += 1
        return True  # Requeued one file; wait for next monitoring iteration

    while True:
        for album_id in list(grab_list.keys()):
            if not slskd_download_status(grab_list[album_id]["files"]):
                grab_list[album_id]["error_count"] = grab_list[album_id].get("error_count", 0) + 1
                continue

            album_done, problems, queued = downloads_all_done(grab_list[album_id]["files"])

            grab_list[album_id].setdefault("count_start", time.time())
            elapsed = time.time() - grab_list[album_id]["count_start"]

            if elapsed >= cfg.slskd.stalled_timeout:
                delete_album("Timeout waiting for download of")
                continue
            if queued == len(grab_list[album_id]["files"]) and elapsed >= cfg.slskd.remote_queue_timeout:
                delete_album("Timeout waiting for download of")
                continue

            if album_done:
                album_data = grab_list[album_id]
                album_data["album_id"] = album_id
                logger.info(f"Completed download of Album: {album_data['title']} Artist: {album_data['artist']}")
                process_completed_album(album_data, failed_grab)
                del grab_list[album_id]
                continue

            if problems:
                logger.debug("Files with errors detected.")
                for file in problems:
                    if album_id not in grab_list:
                        break
                    logger.debug(f"Checking {file['filename']}")
                    state = file["status"]["state"]
                    if state in ("Completed, Cancelled", "Completed, TimedOut", "Completed, Errored", "Completed, Aborted"):
                        if handle_hard_error(album_id, file, problems):
                            break
                    elif state == "Completed, Rejected":
                        if handle_rejected(album_id, file, problems):
                            break
                    else:
                        logger.error(f"Unexpected file state in problem list: {state}")

        if not grab_list:
            break

        time.sleep(5)


def grab_most_wanted(albums):
    """Search, enqueue, monitor, and import every album in `albums`.

    Searches and enqueues downloads for all albums first, then monitors and imports them as a
    batch. Returns the total count of albums that failed to search or failed to grab.
    """

    grab_list, failed_search, failed_grab = search_and_queue(albums)

    total_albums = len(grab_list)
    logger.info(f"Total Downloads added: {total_albums}")
    for album_id in grab_list:
        logger.info(f"Album: {grab_list[album_id]['title']} Artist: {grab_list[album_id]['artist']}")
    logger.info(f"Failed to grab: {len(failed_grab)}")
    for album in failed_grab:
        logger.info(f"Album: {album['title']} Artist: {album['artist']['artistName']}")

    logger.info("-------------------")
    logger.info(f"Waiting for downloads... monitor at: {''.join([cfg.slskd.host_url, cfg.slskd.url_base, 'downloads'])}")

    monitor_downloads(grab_list, failed_grab)

    count = len(failed_search) + len(failed_grab)
    for album in failed_search:
        album_title = album["title"]
        artist_name = album["artist"]["artistName"]
        logger.info(f"Search failed for Album: {album_title} - Artist: {artist_name}")
    for album in failed_grab:
        album_title = album["title"]
        artist_name = album["artist"]["artistName"]
        logger.info(f"Download failed for Album: {album_title} - Artist: {artist_name}")

    return count

def move_failed_import(src_path):
    """Move a failed Lidarr import's folder into a `failed_imports` subfolder, avoiding name clashes."""
    failed_imports_dir = os.path.join(cfg.slskd.download_dir, "failed_imports")

    if not os.path.exists(failed_imports_dir):
        os.makedirs(failed_imports_dir)

    folder_name = os.path.basename(src_path)
    folder_path = os.path.join(cfg.slskd.download_dir, folder_name)
    target_path = os.path.join(failed_imports_dir, folder_name)

    counter = 1
    while os.path.exists(target_path):
        target_path = os.path.join(failed_imports_dir, f"{folder_name}_{counter}")
        counter += 1

    if os.path.exists(folder_path):
        shutil.move(folder_path, target_path)
        logger.info(f"Failed import moved to: {target_path}")

    return os.path.abspath(target_path)


def is_docker():
    return os.getenv("IN_DOCKER") is not None


def migrate_soularr_ini_config(config_dir: str) -> bool:
    """One-time migration from a legacy Soularr config.ini to Soulseekarr's config.yml.

    If config.yml is missing but a Soularr config.ini is present, translates it into a new
    config.yml (mapped to Soulseekarr's schema) so the run can proceed without manual
    reconfiguration. Returns True if a config.yml was written.
    """
    ini_path = os.path.join(config_dir, "config.ini")
    yaml_path = os.path.join(config_dir, "config.yml")

    if os.path.exists(yaml_path) or not os.path.exists(ini_path):
        return False

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ini_path)

    def get(section, option, fallback=""):
        return parser.get(section, option, fallback=fallback) if parser.has_section(section) else fallback

    def get_bool(section, option, fallback):
        return parser.getboolean(section, option, fallback=fallback) if parser.has_section(section) else fallback

    def get_int(section, option, fallback):
        return parser.getint(section, option, fallback=fallback) if parser.has_section(section) else fallback

    def get_float(section, option, fallback):
        return parser.getfloat(section, option, fallback=fallback) if parser.has_section(section) else fallback

    def get_csv(section, option, fallback=""):
        return [item.strip() for item in get(section, option, fallback).split(",") if item.strip()]

    search_source = get("Search Settings", "search_source", "missing").lower().strip()
    sources = ["missing", "cutoff_unmet"] if search_source == "all" else [search_source]

    new_config = {
        "lidarr": {
            "api_key": get("Lidarr", "api_key"),
            "host_url": get("Lidarr", "host_url"),
            "download_dir": get("Lidarr", "download_dir"),
            "disable_sync": get_bool("Lidarr", "disable_sync", False),
            "sources": sources,
            "type": get("Search Settings", "search_type", "first_page").lower().strip(),
            "page_size": get_int("Search Settings", "number_of_albums_to_grab", 10),
            "title_blacklist": get_csv("Search Settings", "title_blacklist"),
            "failed_import_denylist": get_bool("Search Settings", "failed_import_denylist", True),
            "use_selected_lidarr_release": get_bool("Release Settings", "use_selected_lidarr_release", False),
            "use_most_common_tracknum": get_bool("Release Settings", "use_most_common_tracknum", True),
            "allow_multi_disc": get_bool("Release Settings", "allow_multi_disc", True),
            "accepted_countries": get_csv(
                "Release Settings", "accepted_countries", "Europe,Japan,United Kingdom,United States,[Worldwide],Australia,Canada"
            ),
            "skip_region_check": get_bool("Release Settings", "skip_region_check", False),
            "accepted_formats": get_csv("Release Settings", "accepted_formats", "CD,Digital Media,Vinyl"),
        },
        "slskd": {
            "api_key": get("Slskd", "api_key"),
            "host_url": get("Slskd", "host_url"),
            "url_base": get("Slskd", "url_base", "/"),
            "download_dir": get("Slskd", "download_dir"),
            # Soularr's search_timeout is milliseconds; Soulseekarr's timeout is seconds.
            "timeout": max(1, get_int("Search Settings", "search_timeout", 5000) // 1000),
            "maximum_peer_queue": get_int("Search Settings", "maximum_peer_queue", 50),
            "minimum_peer_upload_speed": get_int("Search Settings", "minimum_peer_upload_speed", 0),
            "minimum_filename_match_ratio": get_float("Search Settings", "minimum_filename_match_ratio", 0.5),
            "minimum_search_interval": get_int("Search Settings", "minimum_search_interval", 5),
            "delete_searches": get_bool("Slskd", "delete_searches", True),
            "stalled_timeout": get_int("Slskd", "stalled_timeout", 3600),
            "remote_queue_timeout": get_int("Slskd", "remote_queue_timeout", 300),
            "ignored_users": get_csv("Search Settings", "ignored_users"),
            "search_blacklist": get_csv("Search Settings", "search_blacklist"),
            "album_prepend_artist": get_bool("Search Settings", "album_prepend_artist", False),
            "filtering": get_bool("Download Settings", "download_filtering", False),
            "use_extension_whitelist": get_bool("Download Settings", "use_extension_whitelist", False),
            "extensions_whitelist": get_csv("Download Settings", "extensions_whitelist", "txt,nfo,jpg"),
            "rename_download_folders": get_bool("Download Settings", "rename_download_folders", True),
            "allowed_filetypes": get_csv("Search Settings", "allowed_filetypes", "flac,mp3"),
        },
        "logging": {
            "level": get("Logging", "level", "INFO"),
            "format": get("Logging", "format", DEFAULT_LOGGING_CONF["format"]),
            "datefmt": get("Logging", "datefmt", DEFAULT_LOGGING_CONF["datefmt"]),
            "log_to_file": get_bool("Logging", "log_to_file", True),
            "log_file": get("Logging", "log_file", "soulseekarr.log"),
            "max_bytes": get_int("Logging", "max_bytes", 1048576),
            "backup_count": get_int("Logging", "backup_count", 3),
        },
    }

    class IndentedListDumper(yaml.SafeDumper):
        """Indents list items under their parent key instead of PyYAML's default flush-left "- item" style."""

        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow=flow, indentless=False)

    with open(yaml_path, "w") as yaml_file:
        yaml_file.write("# Auto-generated from config.ini by Soulseekarr's Soularr migration. Review the mapped values below.\n")
        yaml.dump(new_config, yaml_file, Dumper=IndentedListDumper, sort_keys=False, default_flow_style=False)

    # Use warning level so this is visible even though logging isn't configured from config.yml yet.
    logger.warning(f"Migrated legacy Soularr config.ini to {yaml_path}. Please review the generated file.")
    return True


def remove_lock_file(path: str) -> None:
    """Docker doesn't use a lock file, so only remove it outside Docker."""
    if not is_docker() and os.path.exists(path):
        os.remove(path)


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


def get_current_page(path: str, default_page=1) -> int:
    """Read the persisted "incrementing_page" cursor from `path`, creating it with `default_page` if missing."""
    if os.path.exists(path):
        with open(path, "r") as file:
            page_string = file.read().strip()

            if page_string:
                return int(page_string)
            else:
                with open(path, "w") as file:
                    file.write(str(default_page))
                return default_page
    else:
        with open(path, "w") as file:
            file.write(str(default_page))
        return default_page


def update_current_page(path: str, page: str) -> None:
    with open(path, "w") as file:
        file.write(page)


def get_records(source: str) -> list:
    """Fetch Lidarr's wanted records for `source` ("missing" or "cutoff_unmet"), paginated per `lidarr.type`.

    Applies the configured pagination strategy (`all`, `incrementing_page`, or `first_page`),
    then filters out any records that are already in Lidarr's download queue.
    """
    missing = source == "missing"
    try:
        wanted = lidarr.get_wanted(
            page_size=cfg.lidarr.page_size,
            sort_dir="ascending",
            sort_key="albums.title",
            missing=missing,
        )
    except PyarrError as ex:
        logger.error(f"An error occurred when attempting to get records: {ex}")
        return []

    total_wanted = wanted["totalRecords"]

    wanted_records = []
    if cfg.lidarr.type == "all":
        page = 1
        while len(wanted_records) < total_wanted:
            try:
                wanted = lidarr.get_wanted(
                    page=page,
                    page_size=cfg.lidarr.page_size,
                    sort_dir="ascending",
                    sort_key="albums.title",
                    missing=missing,
                )
            except PyarrError as ex:
                logger.error(f"Failed to grab record: {ex}")
            wanted_records.extend(wanted["records"])
            page += 1

    elif cfg.lidarr.type == "incrementing_page":
        page = get_current_page(cfg.current_page_file_path)
        try:
            wanted_records = lidarr.get_wanted(
                page=page,
                page_size=cfg.lidarr.page_size,
                sort_dir="ascending",
                sort_key="albums.title",
                missing=missing,
            )["records"]
        except PyarrError as ex:
            logger.error(f"Failed to grab record: {ex}")
        page = 1 if page >= math.ceil(total_wanted / cfg.lidarr.page_size) else page + 1
        update_current_page(cfg.current_page_file_path, str(page))

    elif cfg.lidarr.type == "first_page":
        wanted_records = wanted["records"]

    else:
        remove_lock_file(cfg.lock_file_path)

        raise ValueError(f"[lidarr.type] - {cfg.lidarr.type = } is not valid")

    try:
        queued_records = lidarr.get_queue(sort_dir="ascending", sort_key="albums.title")
        total_queued = queued_records["totalRecords"]
        current_queue = queued_records["records"]

        if queued_records["pageSize"] < total_queued:
            page = 2
            while len(current_queue) < total_queued:
                try:
                    next_page = lidarr.get_queue(page=page, sort_key="albums.title", sort_dir="ascending")
                except PyarrError as ex:
                    logger.error(f"Failed to get queue details: {ex}")
                    break
                current_queue.extend(next_page["records"])
                page += 1

        queued_album_ids = []

        for record in current_queue:
            if "albumId" in record:
                queued_album_ids.append(record["albumId"])
            else:
                logger.warning(f"Dropping entry due to missing key in keylist: [{record.keys()}]")

        wanted_records_not_queued = []
        for record in wanted_records:
            for release in record["releases"]:
                if release["albumId"] in queued_album_ids:
                    logging.info(f"Skipping record '{record['title']}' because it's already in download queue")
                    break
            else:  # This only runs if the loop is broken out of. Saves on all the boolean found= stuff
                wanted_records_not_queued.append(record)
        if len(wanted_records_not_queued) > 0:
            wanted_records = wanted_records_not_queued
        else:
            logging.info("No records wanted that arent already queued")
            wanted_records = []
    except PyarrError as ex:
        logger.error(f"Failed to get queue details so not filtering based on queue: {ex}")

    return wanted_records


def load_failed_import_denylist(file_path):
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, IOError) as ex:
        logger.warning(f"Error loading failed import denylist: {ex}. Starting with empty denylist.")
        return {}


def save_failed_import_denylist(file_path, denylist):
    try:
        with open(file_path, "w") as file:
            json.dump(denylist, file, indent=2)
    except IOError as ex:
        logger.error(f"Error saving failed import denylist: {ex}")


def add_to_failed_import_denylist(file_path, album_id, artist, title, folder_path=None):
    denylist = load_failed_import_denylist(file_path)
    album_key = str(album_id)
    if album_key not in denylist:
        denylist[album_key] = {
            "album_id": album_id,
            "artist": artist,
            "title": title,
            "failed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "folder_path": folder_path,
        }
        save_failed_import_denylist(file_path, denylist)
        logger.info(f"Added to failed import denylist: {artist} - {title} (ID: {album_id})")


def run_once(args) -> int:
    """Runs a single Soulseekarr cycle: fetch wanted albums, search, download, and import. Returns a process exit code."""
    global cfg, lidarr, slskd, search_cache, folder_cache, broken_user

    lock_file_path = os.path.join(args.var_dir, ".soulseekarr.lock")
    config_file_path = os.path.join(args.config_dir, "config.yml")

    if not is_docker() and os.path.exists(lock_file_path) and args.lock_file:
        logger.info(f"Soulseekarr instance is already running.")
        return 1

    try:
        if not is_docker() and args.lock_file:
            with open(lock_file_path, "w") as lock_file:
                lock_file.write("locked")

        if not os.path.exists(config_file_path):
            migrate_soularr_ini_config(args.config_dir)

        if os.path.exists(config_file_path):
            with open(config_file_path, "r") as config_file:
                raw_config = yaml.safe_load(config_file) or {}
            raw_config = expand_env_vars(raw_config)
            setup_logging(raw_config, args.var_dir)
        else:
            if is_docker():
                logger.error(
                    'Config file does not exist! Please mount "/data" and place your "config.yml" file there. Alternatively, pass `--config-dir /directory/of/your/liking` as post arguments to store the config somewhere else.'
                )
                logger.error("See: https://github.com/cryobry/soulseekarr/blob/main/config.yml for an example config file.")
            else:
                logger.error(
                    "Config file does not exist! Please place it in the working directory. Alternatively, pass `--config-dir /directory/of/your/liking` as post arguments to store the config somewhere else."
                )
                logger.error("See: https://github.com/cryobry/soulseekarr/blob/main/config.yml for an example config file.")
            return 0

        # Load the configuration into a structured object for easier access
        cfg = AppConfig.from_yaml(raw_config, args)

        # Init API clients
        slskd = slskd_api.SlskdClient(host=cfg.slskd.host_url, api_key=cfg.slskd.api_key, url_base=cfg.slskd.url_base)
        lidarr = LidarrAPI(cfg.lidarr.host_url, cfg.lidarr.api_key)

        # Init cache. The wide search returns all the data we need. This prevents us from hammering the users on the Soulseek network
        search_cache = {}
        folder_cache = {}
        broken_user = []

        any_records = False
        total_failed = 0
        sources_to_process = cfg.lidarr.sources

        for source in sources_to_process:
            # Re-resolve cfg for this source so its accepted_formats/allowed_filetypes
            # overrides (and any future per-source settings) apply for this loop only.
            cfg = AppConfig.from_yaml(raw_config, args, source=source)
            logging.debug(f"Getting records from {source}")
            try:
                records = get_records(source)
            except ValueError as ex:
                logger.error(f"An error occurred: {ex}")
                return 0

            if not records:
                continue
            any_records = True

            try:
                filtered = filter_list(records)
                if filtered is not None:
                    total_failed += grab_most_wanted(filtered)
                else:
                    logger.info(f"No releases wanted for source '{source}' that aren't on the deny list and/or blacklisted")
            except Exception:
                logger.exception("Fatal error!")
                return 0

        if any_records:
            if total_failed == 0:
                logger.info("Soulseekarr finished.")
            else:
                logger.info(f"{total_failed}: releases failed to find a match in the search results and are still wanted.")
            if cfg.slskd.remove_completed_downloads:
                slskd.transfers.remove_completed_downloads()
        else:
            logger.info("No releases wanted.")

        return 0

    finally:
        # Remove the lock file after activity is done
        remove_lock_file(lock_file_path)


def get_interval(args) -> int:
    """Resolve the run interval in seconds from --interval, SCRIPT_INTERVAL, or a default.

    CLI `--interval` takes priority, then the `SCRIPT_INTERVAL` env var. Docker defaults to
    300s if neither is set (matching the old run.sh default); non-Docker defaults to running
    once, preserving manual/cron usage.
    """
    if args.interval is not None:
        return args.interval
    env_value = os.environ.get("SCRIPT_INTERVAL")
    if env_value is not None:
        try:
            return int(env_value)
        except ValueError:
            logger.warning(f"Invalid SCRIPT_INTERVAL value: {env_value!r}. Ignoring.")
    return 300 if is_docker() else 0


def main():
    """Parse CLI arguments and run Soulseekarr once or on a loop, per --interval/SCRIPT_INTERVAL."""
    global cfg, lidarr, slskd, logger, search_cache, folder_cache, broken_user

    # Allow some overrides to be passed to the script
    parser = argparse.ArgumentParser(description="""Soulseekarr reads all of your "wanted" albums/artists from Lidarr and downloads them using Slskd""")

    default_data_directory = os.getcwd()

    if is_docker():
        default_data_directory = "/data"

    parser.add_argument(
        "-c",
        "--config-dir",
        default=default_data_directory,
        const=default_data_directory,
        nargs="?",
        type=str,
        help="Config directory (default: %(default)s)",
    )

    parser.add_argument(
        "-v",
        "--var-dir",
        default=default_data_directory,
        const=default_data_directory,
        nargs="?",
        type=str,
        help="Var directory (default: %(default)s)",
    )

    parser.add_argument(
        "--no-lock-file",
        action="store_false",
        dest="lock_file",
        default=True,
        help="Disable lock file creation",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds to wait between runs, looping forever. Falls back to the SCRIPT_INTERVAL env var. Runs once and exits if neither is set (default behavior).",
    )

    args = parser.parse_args()

    interval = get_interval(args)

    if interval > 0:
        while True:
            run_once(args)
            logger.info(f"Waiting {interval} seconds before checking again...")
            time.sleep(interval)
    else:
        sys.exit(run_once(args))


if __name__ == "__main__":
    main()
