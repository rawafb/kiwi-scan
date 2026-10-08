# PVA Scan Data Service

`pva_server` provides EPICS PV Access (PVA) service for reading kiwi-scan data as an EPICS `NTTable`.

The service reads scan files referenced by the newest kiwi-scan manifest.

By default, the server publishes:

```text
KIWI:DATA:SCAN:TABLE
```

A request selects a scan by its index and returns the complete scan file as an `NTTable`.

Scan indices are newest-first:

- `scan_index=0`: latest scan
- `scan_index=1`: previous scan
...

## Starting the server

After installing kiwi-scan (`pip install kiwi-scan[ioc]`) , run:

```bash
kiwi-pva-server
```

Use an explicit data directory:

```bash
kiwi-pva-server --data-dir /path/to/scan-data
```

Set PVA prefix:

```bash
kiwi-pva-server --prefix TEST:KIWI
```

For publishing:

```text
TEST:KIWI:SCAN:TABLE
```

## Command-line options

```text
--data-dir PATH   Manifest/data directory. If omitted, KIWI_SCAN_DATA_DIR
                  and the active-manifest configuration are used.

--prefix PREFIX   PVA endpoint prefix. Default: KIWI:DATA

--log-level 0-5   Set the kiwi-scan logging level.
```

## Requesting a table

### Command-line clients

`pvcall` command-line tool for PVA RPC calls:

```bash
pvcall KIWI:DATA:SCAN:TABLE scan_index=0
```

Previous scan:

```bash
pvcall KIWI:DATA:SCAN:TABLE scan_index=1
```

The default value is `scan_index=0`.

`pvxcall` can be used in the same way:

```bash
pvxcall KIWI:DATA:SCAN:TABLE scan_index=0
```

## Returned `NTTable` Format

Each scan-file column becomes one `NTTable` value array:

- Numeric pandas columns are exported as PVA `double[]` fields.
- Other columns, including timestamps and text, are exported as PVA `string[]` fields.
- Scan column names are stored in the `labels` field.
- The source scan filename is stored in the table `descriptor` field.

For example, a scan with these columns:

```text
Position  TS-ISO8601  DET:VALUE
```

is represented conceptually as:

```text
labels: ["Position", "TS-ISO8601", "DET:VALUE"]
value:
  c0: double[]
  c1: string[]
  c2: double[]
descriptor: "scan-results-20260803.txt"
```

## Data selection

The service uses kiwi-scan manifests to locate scan files:

1. Find the newest manifest.
2. Sort its scan entries newest-first.
3. Select the entry identified by `scan_index`.
4. Load its `data_file` into a pandas `DataFrame`.
5. Convert the `DataFrame` to an EPICS `NTTable`.

The manifest and referenced scan files must be readable by the server process.

## Overview

```text
PVA client
    v
PvaKiwiDataAdapter
    v
ScanReader
    v
ManifestResolver -> DataLoader
    v
pandas DataFrame
    v
DataFrameToNTTableConverter
    v
EPICS NTTable
```

## Requirements

- `p4p` package, including its PVA server and RPC support
- optionally, EPICS Base 7
