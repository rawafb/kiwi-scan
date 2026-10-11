# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

SCAN_ENVVAR = "KIWI_SCAN_SCAN_PATH"

# name -> class
SCAN_REGISTRY: Dict[str, type] = {}

_BUILTINS_REGISTERED = False
# Every external file is executed at most once per process, even if it fails.
_ATTEMPTED_EXTERNAL_SCAN_FILES: Set[Path] = set()


def register_scan_class(name: str, cls: type, *, replace: bool = False) -> None:
    existing = SCAN_REGISTRY.get(name)
    if existing is not None and existing is not cls and not replace:
        raise ValueError( f"Scan type '{name}' is already registered by {existing.__module__}.{existing.__name__}")
    SCAN_REGISTRY[name] = cls


def register_scan(name: Optional[str] = None) -> Callable[[type], type]:
    """
    Decorator for external/private scan engines.

    Example:
        @register_scan("beamline.fast")
        class FastScan(BaseScan):
            ...
    """
    def decorator(cls: type) -> type:
        register_scan_class(name or cls.__name__, cls)
        return cls
    return decorator


def _register_builtin_scan_types() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return

    from kiwi_scan.scan_concrete.approach import ApproachMove
    from kiwi_scan.scan_concrete.cm import CMScan
    from kiwi_scan.scan_concrete.linear import LinearScan

    # from kiwi_scan.scan_concrete.monocm import MonoCMScan
    from kiwi_scan.scan_concrete.para import ParaScan
    from kiwi_scan.scan_concrete.poll import PollScan

    register_scan_class("linear", LinearScan)
    register_scan_class("approach", ApproachMove)
    # register_scan_class("monocm", MonoCMScan)
    register_scan_class("poll", PollScan)
    register_scan_class("para", ParaScan)
    register_scan_class("cm", CMScan)

    _BUILTINS_REGISTERED = True


def _import_external_scan_file(pyfile: Path, raise_on_error: bool = False) -> None:
    pyfile = pyfile.resolve()

    if pyfile in _ATTEMPTED_EXTERNAL_SCAN_FILES:
        return
    _ATTEMPTED_EXTERNAL_SCAN_FILES.add(pyfile)

    digest = hashlib.sha256(str(pyfile).encode("utf-8")).hexdigest()[:12]
    module_name = f"kiwi_scan_ext_scan_{pyfile.stem}_{digest}"
    registry_before = dict(SCAN_REGISTRY)

    try:
        spec = importlib.util.spec_from_file_location(module_name, str(pyfile))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for {pyfile}")

        module = importlib.util.module_from_spec(spec)
        # dataclasses, pickle and typing look the module up by name
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        logger.debug("Imported external scan module: %s", pyfile)

    except Exception:
        # A failed file contributes nothing: drop the module and any scan
        # types it registered before failing.
        sys.modules.pop(module_name, None)
        SCAN_REGISTRY.clear()
        SCAN_REGISTRY.update(registry_before)
        logger.exception("Failed to import external scan module %s", pyfile)
        if raise_on_error:
            raise


def load_all_scan_types(raise_on_error: bool = False) -> None:
    """
    Load built-in scan types, then load external/private scan modules from
    KIWI_SCAN_SCAN_PATH.

    KIWI_SCAN_SCAN_PATH may contain:
      - one or more directories
      - one or more individual .py files

    Entries are separated by os.pathsep (':' on Linux).
    """
    _register_builtin_scan_types()

    raw = os.environ.get(SCAN_ENVVAR, "")
    if not raw:
        return

    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue

        path = Path(entry).expanduser()

        if not path.exists():
            logger.warning("Scan path does not exist: %s", path)
            continue

        if path.is_file():
            if path.suffix == ".py" and not path.name.startswith("_"):
                _import_external_scan_file(path, raise_on_error=raise_on_error)
            continue

        for pyfile in sorted(path.glob("*.py")):
            if pyfile.name.startswith("_"):
                continue
            _import_external_scan_file(pyfile, raise_on_error=raise_on_error)

def get_available_scan_types() -> List[str]:
    load_all_scan_types()
    return sorted(SCAN_REGISTRY.keys())
