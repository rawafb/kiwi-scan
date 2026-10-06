# Changelog

All notable changes to `kiwi-scan` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- Added a point pipeline that owns point frames, caches, column construction.
- Added an absolute timer to `SyncController` for scans without external `sync` subscriptions.
- Added `detector_reader_strategy` for `direct` or atomic local-cache `snapshot` acquisition strategies.
- Added `timestamp_output_format` for `iso8601` and `unix` time-stamp file output selection.
- Added reusable actuator helpers: `create_actuators()`, `load_actuators()`, `run_monitors()`, and `MonitorSpec`.
- Added the `kiwi-pva-server` cli and PVA `NTTable` (scan data) service.
- Added --last-scan option to manifestfiles cli
- Added the `kiwi-config-defaults` cli to print the full ScanConfig YAML structure with default values.

### Changed

- Scan point data writer now runs through a background writer task.
- Plugin columns no longer add one scan-file timestamp column per value.
  Detector and plugin timestamps are available as raw POSIX values in the runtime point caches.
- Plugin constructors now receive `ScanPluginContext`, a subset of the  `BaseScan` API.
- The `cm` and `poll` scan types use a positive `steps` value as a maximum acquired-point count. 
Non-positive values leave termination as before to motion, range, or stop conditions.
- Cleanup: data and export APIs moved to `kiwi_scan.data` and `kiwi_scan.export`; running statistics moved to `kiwi_scan.tools`.
- Trigger writes no longer inherit an implicit EPICS queueing delay. 
- Avoiding timestamp formatting in the DAQ hot path.
- Improved error handling

### Fixed

- CAN undulator velocities encoding in unsigned 16-bit fields.

### Migration notes

- Replace `kiwi_scan.dataloader` and `kiwi_scan.metadata_loader` imports with imports from `kiwi_scan.data`.
- Replace `kiwi_scan.io` imports with imports from `kiwi_scan.export`.
- Replace `kiwi_scan.stats` imports with imports from `kiwi_scan.tools`.
- Plugins must use the `ScanPluginContext` protocol.

## [0.5.1] - 2026-07-30

### Added

- Added optional scan performance reporting to CM scan type.
- Added `TimestampPerformancePlugin` for detector timestamp diagnostics.
- Added reusable timestamp conversion for the time stamp data columns.
- Added metadata-monitor queue diagnostics.
- Added support for endless motion to sim actuator

### Changed

- Scan and subscription configurations are now validated before scan startup.
- Metadata monitor timestamps and numeric PV timestamps are consitently written in UTC.
- Cleaned up monitor backend tests

### Fixed

- Fixed incomplete current-row cache updates in `poll` and `para` scans.

---

## [0.5.0] - 2026-07-23

### Added

- Added AsyncScanPlugin base class for non-blocking background plugin processing.
  - Supports synchronous and asynchronous output columns.
  - Provides pending/error fallback handling
  - Drops new snapshots while a worker is busy.
- Expanded the generic scan IOC API.
  - Added `Config` and `ScanType` records for changing the scan configuration.
  - Added a `LogLevel` record for changing the Python logging level at runtime (`notset`, `debug`, `info`, `warning`, `error`, or `critical`).
  - Added a `kill` record for shutting down the IOC.
- Added scan-data export framework: `kiwi_scan.io`.
  - Added loaders for a scan file, an explicit manifest, or the latest manifest.
  - Added relocation handling for manifest references with invalid absolute paths.
  - Added SPEC export support and cli (kiwi2spec).

### Changed

- Made `QueuePlotterMonitor` check for headless systems.
- Improved standard scan execution and `scan_runner` error handling.
- Improved generic scan IOC error reporting.
- Metadata timestamps are now consitently parsed as UTC values.
- Simplified `EpicsPV.get()` and removed retry to avoid execution time spikes.
- Moved command-line code to `kiwi_scan.cli`.
- Changed the default plugin-specific logging level to `WARNING`.

### Fixed

- Fixed loading metadata sidecar files that contain monitor rows but no `metadata_constants` section.
- Fixed IOC error-state feedback and record alarms for failed scans.
- Fixed QueuePlotterMonitor startup on systems without a graphical display.
- Hardened command line tools for exceptions.

---

## [0.4.0] - 2026-06-12

### Added

- Added a generic **pythonSoftIOC-based scan IOC** with unit tests.
    - New `scanioc` command-line tool for exposing kiwi-scan scans as EPICS records.
    - IOC controller API separated from the softIOC
    - Supports publishing scan state, position, output file, and arbitrary scan values as EPICS PVs.
    - Unit tests.
- Added **actuator startup wait support** for continuous scans.
    - New actuator startup detection.
    - Improves synchronization of data acquisition with continuously moving devices.
- Added **manifest archive/delete planning API**.
    - `ManifestResolver` can now plan deletion bundles including referenced scan and metadata files.
    - Archive-before-delete workflows.
    - CLI support.
- Added `manifestfiles_cli --create`.
    - Generates compact ("small") manifest files referencing existing scan data files.
    - Useful for external data import workflows and format conversion tools such as SPEC export (unpublished yet).

### Changed

- Refactored monitor infrastructure with a shared formatting layer.
    - Introduced `MonitorRowFormatter` and `MonitorValueFormatter` shared by print and plotting monitors.
    - Identical formatting and output behavior across monitor implementations.
- Enhanced **QueuePlotterMonitor**.
    - Supports multiple independent live plot panels from YAML configuration.
    - Supports plotting multiple Y channels in a single panel.
    - Can optionally emit the same machine-readable output stream as the PrintMonitor.
- Monitor configuration format.
    - Print and plot monitor settings are now grouped under the common `monitor:` configuration block.

### Fixed

- Fixed CM scan handling of actuator backlash and continuous-motion startup behavior.
    - Improved scan range entry/exit detection when backlash compensation is used.
    - Added startup ramp before DAQ processing begins.
- Fixed cleanup of original actuator velocities in CM scans.
    - Original velocities are now restored reliably when a scan finishes or is stopped via `scan.stop()` / Ctrl+C.

### Internal

- Added current-row cache for plugin calculations

## [0.3.0] - 2026-05-22

### Added

- Added running online statistics utilities for multiple subscriptions (role=`stat`)
- Added a new CLI tool to monitor configured subscription streams in real time including statistics (mean, standard deviation, minimum, maximum, number of collected samples)
- Add new `para` scan type for passive parasitical step scans.
  The engine observes externally moved actuators, waits for them to enter the configured scan range, waits for a running/ready transition cycle, and records stable points using the standard DAQ, trigger, plugin, monitor, metadata, and statistics pipeline.
- Added ctrl+c support to scan_runner. First ctrl+c will stop all motors and exit the scan loop immediately.
- Added documentation for SyncController
- Added documentation for kiwi statistics

### Changed
- Fixed timestamp handling so scan and metadata files use local timezone-aware timestamps, and plot/data loaders preserve the stored timezone instead of converting timestamps to UTC.
- Add sample_rate_hz to scan config and BaseScan. Calculating rate from steps in dimensions is depricated.
- mkvenv.sh now finds the kiwi-scan repository root from the script path, so it can be sourced from any working directory. A KIWI_SCAN_REPO_ROOT override is available for custom layouts.
- Improved monitor-based EPICS data acquisition using cached CA monitor reads with polling fallback with adjustable rate.
- Linear and Parasitcal step scans now maintain scalable online sync statistics during DAQ operation (mean, standard deviation, minimum, maximum, number of collected samples)
- Warn now when velocity cannot be restored in cm scan type.
- Fixed manifests for missing active manifests
---

## [0.2.1] - 2026-05-15

### Added
- Added documentation for the trigger manager.
- Added documentation for the plugin system.
- Added documentation for the API.
- Added documentation for the actuator framework.
- Added documentation for the data models and YAML reference.
- Added documentation for kiwi scan types.
- Added documentation for vim syntax.
- Added basic version of vim syntax highlighting.
- Added `scantrigger_cli` for executing configured trigger phases from the command line.
- Enhanced LoggingPlugin: Alarm log, actuator trace, point to point timing.

### Changed
- Improved EPICS actuator waits: short CA disconnects are handled as unknown status instead of motion.
- Improved actuator_runner: Stop active actuators on interrupt, forced exit on second Ctrl-C.
- Include temporary manifest files in scan file lists.
---

## [0.2.0] - 2026-05-06

### Added
- First public PyPI release of `kiwi-scan`.
- Modular scan framework for EPICS-based commissioning and diagnostics.
- YAML-based scan configuration.
- Support for scan engines, actuators, detector PVs, plugins, triggers, subscriptions, metadata sidecars, and manifest files.
- Command-line tools for running scans and inspecting scan results.
