from __future__ import annotations

import configparser
import logging
import os

import yaml

logger = logging.getLogger("soulseekarr")

DEFAULT_LOGGING_CONF = {
    "level": "INFO",
    "format": "[%(levelname)s|%(name)s|L%(lineno)d] %(asctime)s: %(message)s",
    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
    "log_to_file": True,
    "log_file": "soulseekarr.log",
    "max_bytes": 1048576,
    "backup_count": 3,
}


def expand_env_vars(value):
    """Recursively expand environment references in values loaded from YAML."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def migrate_soularr_ini_config(config_dir: str) -> bool:
    """Migrate a legacy Soularr config.ini to Soulseekarr's config.yml once."""
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
    new_config = {
        "lidarr": {
            "api_key": get("Lidarr", "api_key"),
            "host_url": get("Lidarr", "host_url"),
            "download_dir": get("Lidarr", "download_dir"),
            "import_timeout": get_int("Lidarr", "import_timeout", 3600),
            "disable_sync": get_bool("Lidarr", "disable_sync", False),
            "sources": ["missing", "cutoff_unmet"] if search_source == "all" else [search_source],
            "search_type": get("Search Settings", "search_type", "incrementing").lower().strip(),
            "chunk_size": get_int("Search Settings", "number_of_albums_to_grab", 10),
            "title_blacklist": get_csv("Search Settings", "title_blacklist"),
            "failed_import_denylist": get_bool("Search Settings", "failed_import_denylist", True),
            "use_selected_lidarr_release": get_bool("Release Settings", "use_selected_lidarr_release", False),
            "use_most_common_tracknum": get_bool("Release Settings", "use_most_common_tracknum", True),
            "allow_multi_disc": get_bool("Release Settings", "allow_multi_disc", True),
            "accepted_countries": get_csv("Release Settings", "accepted_countries", "Europe,Japan,United Kingdom,United States,[Worldwide],Australia,Canada"),
            "skip_region_check": get_bool("Release Settings", "skip_region_check", False),
            "accepted_formats": get_csv("Release Settings", "accepted_formats", "CD,Digital Media,Vinyl"),
        },
        "slskd": {
            "api_key": get("Slskd", "api_key"),
            "host_url": get("Slskd", "host_url"),
            "url_base": get("Slskd", "url_base", "/"),
            "download_dir": get("Slskd", "download_dir"),
            "timeout": max(1, get_int("Search Settings", "search_timeout", 5000) // 1000),
            "maximum_peer_queue": get_int("Search Settings", "maximum_peer_queue", 50),
            "minimum_peer_upload_speed": get_int("Search Settings", "minimum_peer_upload_speed", 0),
            "minimum_filename_match_ratio": get_float("Search Settings", "minimum_filename_match_ratio", 0.5),
            "minimum_search_interval": get_int("Search Settings", "minimum_search_interval", 5),
            "remove_searches": get_bool("Slskd", "remove_searches", True),
            "stalled_timeout": get_int("Slskd", "stalled_timeout", 3600),
            "remote_queue_timeout": get_int("Slskd", "remote_queue_timeout", 300),
            "ignored_users": get_csv("Search Settings", "ignored_users"),
            "search_blacklist": get_csv("Search Settings", "search_blacklist"),
            "album_prepend_artist": get_bool("Search Settings", "album_prepend_artist", False),
            "filtering": get_bool("Download Settings", "download_filtering", False),
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
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow=flow, indentless=False)

    with open(yaml_path, "w") as yaml_file:
        yaml_file.write("# Auto-generated from config.ini by Soulseekarr's Soularr migration. Review the mapped values below.\n")
        yaml.dump(new_config, yaml_file, Dumper=IndentedListDumper, sort_keys=False, default_flow_style=False)

    logger.warning(f"Migrated legacy Soularr config.ini to {yaml_path}. Please review the generated file.")
    return True
