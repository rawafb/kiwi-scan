[![PyPI version](https://img.shields.io/pypi/v/kiwi-scan.svg)](https://pypi.org/project/kiwi-scan/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20662095.svg)](https://doi.org/10.5281/zenodo.20662095)

# kiwi-scan

`kiwi-scan`: A Modular Scan Framework for Commissioning and Diagnostics in EPICS Environments

Actuators, detector PVs, triggers, subscriptions, plugins, and metadata sidecars are configured via YAML. 
Scan engine (scan type) and scan dimensions are chosen by command line or API. 
Results are written to timestamped text files. Optional metadata sidecars can record constants and monitored PVs in parallel.

## Overview of Features

-   **YAML configuration** for actuators, detectors, scan dimensions, triggers, metadata PVs/constants, subscriptions, plots, and plugin parameters.
-   **Pluggable scan engines** such as `linear`, `approach`, `poll`, `para`, and `cm`, plus externally registered scan types.
-   **Pluggable runtime extensions** - Plugins hook into scan logic and events and can add computed columns or act on monitor events. 
      Base classes are provided for synchronous and asynchronous plugin processing.
- **Column provider** framework for providing extra data from scan engines with build in statistical calculations 
-   **Synchronized acquisition** from multiple sources, with an absolute timer fallback when no external synchronization PV is configured.
-   **Selectable detector acquisition** using direct reads or atomic snapshots of a monitor-fed local cache.
-   **High-rate point pipeline** that assembles provider, detector, and plugin values while ordered file output runs through a background writer via queue.
-   **EPICS integration** via a pyepics wrapper, or a **simulated actuator backend** for tests and development.
-   **Event handling** - Subscriptions route monitored events into defined roles.
-   **Triggers** - Triggers run configured PV writes at named scan phases or on monitor events.
-   **Flexible data output** with timestamped tab-separated scan files, ISO-8601 or Unix timestamps, optional per-detector timestamps, metadata sidecars, waveform support, 
      and runtime enable/disable control.
-   **Data Export** - Extensible export framework for converting kiwi-scan data to external formats, with built-in support for the SPEC file format.
-   **Manifest-based file management** for grouping, listing, archiving, and deleting scan-related files, including selection of the most recent scans.
-   **Monitoring, diagnostics, and visualization** with machine-readable console output, live and post-mortem plotting, online statistics,
      and built-in **performance measurements**.
-   **Control-system integration** through a generic EPICS pythonSoftIOC **scan IOC** and a **PVA server** that publishes scan data as `NTTable` structures.
-   **Python and command-line APIs**

## Installation

Install the latest released version from PyPI:

```bash
pip install kiwi-scan
```

Upgrade:
```bash
pip install --upgrade kiwi-scan
```

Install IOC support

```bash
pip install "kiwi-scan[ioc]"
```

Install a specific version

```bash
pip install "kiwi-scan[ioc]==0.6.0"
```

For development, clone the repository and install it in editable mode:

```bash
pip install -e ".[dev,ioc]"
```

## Dependencies

kiwi-scan depends on pyepics which depends on Epics Base Version 3.14.12 or higher.
Installing the bash completions package (e.g. Debian: ```sudo apt install bash-completion```) is highly recommended for the CLI tools.

## Quick start

The example below runs a tiny detector-free scan with a simulated actuator and writes the output into the current directory.

Create `sim_minimal.yaml`:

```yaml
actuators:
  theta:
    type: sim
    pv: THETA
    rb_pv: THETA:RBV
    velocity: 1.0
    dwell_time: 0.0

detector_pvs: []
data_dir: .
output_file: sim_scan.txt
include_timestamps: true
timestamp_output_format: iso8601
detector_reader_strategy: direct
```

Run a 5-point linear scan:

```bash
export KIWI_SCAN_DATA_DIR="$PWD"

scan_runner \
  --scan_type linear \
  --config-file ./sim_minimal.yaml \
  --dim actuator=theta,start=0,stop=1,steps=5
```

- `scan_runner` loads the YAML file.
- The `--dim` arguments define the actual scan range for this run.
- A timestamped file such as `sim_scan-20260401123045.txt` is created. If the file exists, a unique id is created.
- Even without detectors, the file still contains the scan position and scan timestamp columns.

### Logging

Command-line tools support a `--log-level` argument for setting the logging level using MBBO-style values:

`0` = NOTSET, `1` = DEBUG, `2` = INFO, `3` = WARNING, `4` = ERROR, `5` = CRITICAL.

## Scan Types

kiwi-scan provides a set of built-in scan engines for common beamline and commissioning workflows, including point-by-point scans, continuous motion scans, polling-based acquisition, and nonlinear approach trajectories. In addition, users can implement and register custom scan types through the scan registry API.

For detailed description check the kiwi scan type documentation
[Kiwi Scan types](https://github.com/hz-b/kiwi-scan/blob/master/docs/scans.md)

Built-in performance measurement can measure scan operations.

See [Performance testing](https://github.com/hz-b/kiwi-scan/blob/master/docs/performance.md)

The scan point lifecycle, detector snapshot strategies, runtime caches, and
background writer are described in the [point-pipeline documentation](https://github.com/hz-b/kiwi-scan/blob/master/docs/point-pipeline.md).

## SyncController


The SyncController is used by scan engines to synchronize the scan cycle with multiple synchronized EPICS monitor updates.
For detailed description check the SyncController documentation
[SyncController](https://github.com/hz-b/kiwi-scan/blob/master/docs/sync-controller.md)

## Stats and Column Provider

The kiwi-scan supports statistics for scan types based on BaseScan and provides a command line tool for live statistics from configured subscriptions.
For detailed description check the statistics documentation
[Stats](https://github.com/hz-b/kiwi-scan/blob/master/docs/stats.md)


## YAML Configuration

`kiwi-scan` uses YAML files to configure actuators, detector PVs, monitors, triggers, subscriptions, plugins, metadata, and scan-related defaults.

The scan dimensions are normally added by the API or command line arguments (`--dim`). A scan object always needs at least one scan dimension.

Unknown fields in dataclass-based YAML blocks are generally ignored during parsing. This makes configuration files more forward-compatible across versions, but it also means that misspelled optional fields may not fail immediately.

See the full [YAML reference](https://github.com/hz-b/kiwi-scan/blob/master/docs/yaml.md) for the description.

## The Actuator Framework

The actuator framework provides a small interface for motion-like devices defined in `AbstractActuator`. 

Concrete backends implement the real hardware access, for example EPICS PVs or an undulator-specific multi-axis actuator.

The `actuator_runner`is a command line tool useful for EPICS monitor based debugging, testing and shell scripting. 
Typical use:

```bash
actuator_runner --config-file ./mono.yaml --move energy=400
```

The most important files are:

```text
actuator/
  single.py          # common actuator interface and PV event model
  multi.py           # composite actuator for multiple child axes

actuator_concrete/
  single_epics.py    # EPICS implementation of a single actuator
  undulator.py       # two-axis undulator actuator helpers

actuator_runner.py   # command-line tool for direct actuator operation
```
A more detailed description can be found [here](https://github.com/hz-b/kiwi-scan/blob/master/docs/actuator.md). 

## Monitors

For details on the print and live queue plotter monitors, including output formats, YAML configuration, and multi-panel plotting, see [monitor.md](https://github.com/hz-b/kiwi-scan/blob/master/docs/monitor.md).

## Public API

`kiwi-scan` can be embedded directly as a Python library, for example inside a Python IOC or another beamline control application.

The command-line tools use the library API: they build a `ScanConfig`, load scan/plugin implementations, and then create or execute a scan object.

Check the public API documentation described [here](https://github.com/hz-b/kiwi-scan/blob/master/docs/api.md).

## Plugin developer description and example

Plugins are instantiated from `plugin_configs` and discovered from the built-in plugin package plus any files or directories listed in `KIWI_SCAN_PLUGIN_PATH`.
New plugin classes must be derived from the interface defined in [plugin base class](https://github.com/hz-b/kiwi-scan/blob/master/src/kiwi_scan/plugin/base.py) or from the [async plugin base class](https://github.com/hz-b/kiwi-scan/blob/master/src/kiwi_scan/plugin/async_base.py)

For detailed description check the plugin user and developer documentation:

[Plugins](https://github.com/hz-b/kiwi-scan/blob/master/docs/plugins.md) - plugin API, plugin discovery, YAML configuration, and built-in plugins such as `LoggingPlugin` and `JogPidPlugin`.

[Async Plugins](https://github.com/hz-b/kiwi-scan/blob/master/docs/async_plugins.md) - async plugin extension for parallel background processing.

## IOC

For running kiwi-scan as a generic EPICS soft IOC, see [ioc.md](https://github.com/hz-b/kiwi-scan/blob/master/docs/ioc.md).

## PVA Server

kiwi-scan provides a command line tool (`kiwi-pva-server`) to start a PVA service for receiving scan data referenced by the manifest index. See the [PVA server documentation](https://github.com/hz-b/kiwi-scan/blob/master/docs/pva_server.md).

## kiwi2spec Data Export

kiwi2spec converts one or more kiwi-scan data files, including associated metadata, into the SPEC file format for the use with existing analysis and visualization tools.
Established EPICS-community tools can further convert SPEC data into formats such as NeXus.

[kiwi2spec](https://github.com/hz-b/kiwi-scan/blob/master/docs/kiwi2spec.md) 

## Command-line tools

After installation, the command line tools are available:

- `scan_runner` - execute scans from YAML + CLI dimensions + other options
- `actuator_runner` - actuator commands and run optional monitors + formatted output
- `scanplotter_cli` - plot scan data, optionally use manifest and file index
- `pollstats_cli` - online statistics without scan from YAML config
- `scantrigger_cli` - execute triggers from YAML config
- `manifestfiles` - list, create, archive, or delete manifest-related files
- `scanioc` - run the generic scan IOC based on pythonSoftIOC
- `kiwi-pva-server` - run the PVA server
- `kiwi-convert` - convert scan data through the generic export framework
- `kiwi2spec` - export one or more scan data files, including metadata, to SPEC
- `kiwi-config-defaults` - print the full ScanConfig YAML structure with default values

Examples:

```bash
scan_runner --help
actuator_runner --help
scanplotter_cli --help
scantrigger_cli --help
pollstats_cli --help
manifestfiles --help
scanioc --help
kiwi-pva-server --help
kiwi-convert --help
kiwi2spec --help
kiwi-config-defaults --help
```
See the [Makefile helpers](#makefile-helpers) section for information how to install the bash completion scripts.

## Output files

For details about manifest files, scan data files, and metadata sidecars, see [Output files](docs/datafiles.md).

## Environment variables

- `KIWI_SCAN_DATA_DIR` — base directory for output files
- `KIWI_SCAN_MANIFEST_FILE` – explicitly set the active manifest file
- `KIWI_SCAN_MANIFEST_STATE_FILE` – override the active manifest state file path
- `KIWI_SCAN_REPLACE_*` — placeholder replacement values for YAML templates
- `KIWI_SCAN_CONFIG_DIR` — where preset YAML configs are searched
- `KIWI_SCAN_PLUGIN_PATH` — extra plugin files/directories to import
- `KIWI_SCAN_SCAN_PATH` — extra scan-type files/directories to import

See [examples/beamline_env.sh](https://github.com/hz-b/kiwi-scan/blob/master/examples/beamline_env.sh) for a setup example.

## Docs

User and developer documentation:

- [Actuator framework](https://github.com/hz-b/kiwi-scan/blob/master/docs/actuator.md) - small common abstract interface to actuator like devices. 
- [YAML reference](https://github.com/hz-b/kiwi-scan/blob/master/docs/yaml.md) - data model description. 
- [API reference](https://github.com/hz-b/kiwi-scan/blob/master/docs/api.md) - public API description. 
- [Trigger manager](https://github.com/hz-b/kiwi-scan/blob/master/docs/trigger-manager.md) - scan trigger phases, YAML configuration, and trigger execution.
- [Plugins](https://github.com/hz-b/kiwi-scan/blob/master/docs/plugins.md) - plugin API, YAML configuration, and built-in plugins such as `LoggingPlugin` and `JogPidPlugin`.
- [Scan Types](https://github.com/hz-b/kiwi-scan/blob/master/docs/scans.md) - provided and custom kiwi scan types.
- [vim](https://github.com/hz-b/kiwi-scan/blob/master/docs/vim.md) - simple syntax highlighting setup
- [Stats](https://github.com/hz-b/kiwi-scan/blob/master/docs/stats.md) - kiwi statistics support
- [SyncController](https://github.com/hz-b/kiwi-scan/blob/master/docs/sync-controller.md) - kiwi synced scan loops
- [IOC](https://github.com/hz-b/kiwi-scan/blob/master/docs/ioc.md) - kiwi scan IOC
- [PVA Server](https://github.com/hz-b/kiwi-scan/blob/master/docs/pva_server.md) - PVA server capability and command-line tool
- [Converter](https://github.com/hz-b/kiwi-scan/blob/master/docs/kiwi2spec.md) - data export
- [Performance testing](https://github.com/hz-b/kiwi-scan/blob/master/docs/performance.md) - performance reports
- [Point pipeline](https://github.com/hz-b/kiwi-scan/blob/master/docs/point-pipeline.md) - detector acquisition, point assembly, caches, and persistence
- [README.md](https://github.com/hz-b/kiwi-scan/blob/master/README.md) - current readme

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/hz-b/kiwi-scan)

## Development setup

For repository development, a top-level `Makefile` and `mkvenv.sh` helper script are provided.

### Activate the development environment

```bash
source ./mkvenv.sh
```

`mkvenv.sh` must be **sourced**, not executed. It:

- creates `.venv` if it does not exist
- activates `.venv`
- upgrades `pip` and installs build helpers on first setup
- installs `kiwi-scan` in editable mode with development extras
- prefixes the shell prompt with `KIWI` so the active development shell is obvious

If you want the environment to remain active in your current shell, always use `source ./mkvenv.sh` directly. `make` runs recipes in subprocesses and cannot keep your interactive shell activated.

### Makefile helpers

Use the self-documenting help target to see the available development commands:

```bash
make help
```

The development targets are:

- `make help` - show help
- `make lint` - run `pylint` on `src/kiwi_scan`
- `make test` - run Python unit tests
- `make install_completion` - install bash completion snippets from `bash-completion/`
- `make uninstall_completion` - remove installed bash completion snippets
- `make cscope` - build `cscope` and `ctags` indexes used by vim.
- `make tag` - create a timestamp-based tag from `HEAD`
- `make clean` - remove `.venv`, caches, tags, and generated metadata such as `*.egg-info`

## Contributing

### Tagging
We use a dark launch strategy: features are deployed continuously but activated separately.
Timestamped tags (e.g. 0.1.1+20260424.094820) are mapped to a tagged integration state.
Public releases use clean semantic versions (X.Y.Z) on PyPI, each mapped to a time stamped tag.

### Guidelines
- Read the development setup section.
- Keep changes focused and small.
- Run `make test` locally.
- Tests should be added in `tests/`
- Use `logging` for diagnostics.
- For debugging use the --log-level switch. Include log output and config for reporting bugs.

## Citation

Cite the Zenodo concept DOI:

[DOI 10.5281/zenodo.20662095](https://doi.org/10.5281/zenodo.20662095)


The `v0.4.0` release has its own version DOI on Zenodo.

[DOI 10.5281/zenodo.20662096](https://doi.org/10.5281/zenodo.20662096)


## References

- **KIWI Scan Presentation**  
  https://indico.in2p3.fr/event/37441/contributions/171970/attachments/101030/156847/kiwi-scan.pdf

- **HASMI: A Configurable Python Framework for Automated Harmonic Analysis and Scan Orchestration at EMIL**  
  A. Balzer, A. Efimenko, P. Sreelatha Devi, K. Holldack,  
  Helmholtz-Zentrum Berlin (HZB), Berlin, Germany,  
  *Proceedings of ICALEPCS 2025*, THAG004 (2025).  
  https://proceedings.jacow.org/icalepcs2025/pdf/THAG004.pdf

## License

This project is licensed under the MIT License. See the LICENSE file for details.
