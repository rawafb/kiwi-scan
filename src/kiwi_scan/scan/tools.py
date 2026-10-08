# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

import kiwi_scan
from kiwi_scan.datamodels import ScanConfig
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.registry import SCAN_REGISTRY, load_all_scan_types
from kiwi_scan.yaml_loader import yaml_loader

logger = logging.getLogger(__name__)

def is_valid_logging_level(level):
    """Check if the given integer is a valid logging level."""
    return level in [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]

def set_valid_logging_level(level):
    """
    Converts mbbo record values 0..5 to logging levels and checks if valid.
    If level is not an integer, it prints an error message.
    """
    try:
        level = int(level)
    except (ValueError, TypeError):
        print(f"Invalid logging level (not an integer): {level}")
        return

    scaled_level = level * 10
    if is_valid_logging_level(scaled_level):
        logging.getLogger().setLevel(scaled_level)  # Set logger level using the scaled value
        logger.info(f"Logging level set to {logging.getLevelName(scaled_level)}")
    else:
        logger.error(f"Invalid logging level ({level})!")

def get_kiwi_config_dir_from_environ():
    """
    Returns the absolute path from KIWI_SCAN_CONFIG_DIR if defined and is a valid directory,
    otherwise returns None.
    """
    dir_path = os.environ.get("KIWI_SCAN_CONFIG_DIR")
    if dir_path and os.path.isdir(dir_path):
        return os.path.abspath(dir_path)
    return None

def get_scan_config_dir():
    """
    Returns the absolute path to the 'scan_config' directory.
    If KIWI_SCAN_CONFIG_DIR is defined (and valid), returns its 'scan_config' subdirectory;
    otherwise returns the original default path.
    """
    kiwi_scan_config_dir = get_kiwi_config_dir_from_environ()
    if kiwi_scan_config_dir is not None:
        # If the environment variable is set, override the default
        return os.path.normpath(kiwi_scan_config_dir)
    # fallback
    pkg_dir = Path(kiwi_scan.__file__).resolve().parent
    return str(pkg_dir.parent / ".." / "config" / "scan_config")

def load_scan_config_from_file(config_dir, file_name, replacements) -> Dict[str,Any]:
        fn = os.path.join(config_dir, f"{file_name}")
        return yaml_loader(fn, replacements)

def load_scan_configs(config_dir, replacements):
    """
    Load scan configurations from YAML files in the specified directory.
    Incompatible files are skipped with warning.
    Args:
        config_dir (str): Path to the directory containing YAML configuration files.
        replacements (dict)
    Returns:
        dict: A dictionary where keys are configuration names and values are ScanConfig objects.
    Raises:
        FileNotFoundError: If the configuration directory does not exist.
    """
    scan_configs = {}
    if not os.path.exists(config_dir):
        raise FileNotFoundError(f"Configuration directory '{config_dir}' does not exist.")

    for file_name in os.listdir(config_dir):
        if file_name.endswith(".yaml"):
            try:
                config_data = load_scan_config_from_file(config_dir, file_name, replacements)
                if not isinstance(config_data, dict):
                    raise TypeError("top level is not a mapping")
                scan_config = ScanConfig.from_dict(config_data)
            except Exception as exc: # noqa: BLE001
                logger.warning("Skipping scan config %s: %s", os.path.join(config_dir, file_name), exc)
                continue
            config_name = os.path.splitext(file_name)[0]
            scan_configs[config_name] = scan_config
    return scan_configs

def create_scan_with_config(
    scantype: str,
    config: ScanConfig,
    data_dir: Optional[str] = None) -> Optional[BaseScan]:

    """
    Scan factory
    """
    logger.debug("Updated ScanConfig: %s", yaml.safe_dump(asdict(config), sort_keys=False))

    load_all_scan_types()

    try:
        scan_class = SCAN_REGISTRY[scantype]
    except KeyError:
        logger.error(
            "Unknown scan type '%s'. Available scan types: %s",
            scantype,
            ", ".join(sorted(SCAN_REGISTRY.keys())),
        )
        return None

    try:
        scan = scan_class(config, data_dir)
    except Exception as e: # noqa: BLE001
        logger.error(f"Failed to create scan object [{scantype}]: {e}")
        return None

    return scan

def scan_with_config(
    scantype: str,
    config: ScanConfig,
    data_dir: Optional[str] = None) -> Optional[BaseScan]:

    logger.debug(f"Scan_with_config: {config}")
    scan = create_scan_with_config(scantype, config, data_dir)
    if scan:
        scan.execute()
    return scan

