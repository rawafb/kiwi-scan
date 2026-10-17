# Plugins

Plugins are extensions for kiwi-scan. The hook into the scan logic, react to monitor events routed through subscriptions and can add extra columns to the scan output

Plugins are configured in YAML under the top-level `plugin_configs:` section.

## Plugin concept

A plugin is a Python class derived from `ScanPlugin`.

Plugins can:

- add additional control logic to the scan engine
- add computed values to each scan point
- read additional PVs or actuator state
- log diagnostics during a scan
- react to subscription events through `on_monitor()`
- access the supported scan context, including actuators and point values,
  through `self.scan`

## Plugin discovery

Built-in plugins are loaded by kiwi-scan. External plugins can be registered from directories listed in `KIWI_SCAN_PLUGIN_PATH`.

Example:

```bash
export KIWI_SCAN_PLUGIN_PATH="$PWD/plugins"
```

A plugin must register itself with `register_plugin()`:

```python
from kiwi_scan.plugin.registry import register_plugin
from kiwi_scan.plugin.base import ScanPlugin

@register_plugin("MyPlugin")
class MyPlugin(ScanPlugin):
    ...
```

The registered name is the value used in YAML as `type:`.

## `plugin_configs` YAML section

Example:

```yaml
plugin_configs:
  - type: LoggingPlugin
    name: scan_log
    parameters:
      enable_alarm_trace: True
      enable_point_timing: True
      enable_actuator_trace: True
      alarm_log:
        - TEST:DET:STATUS
        - TEST:MOTOR:STATUS

  - type: JogPIDPlugin
    name: jog_pid
    parameters:
      sample_time: 0.5
      kp: 0.001
      ki: 0.0
      kd: 0.0
      kvf: 0.0
      actuator:
        type: epics
        pv: TEST:JOG:SET
        rb_pv: TEST:JOG:RBV
        velocity_pv: TEST:JOG:VELO
        jog:
          velocity_pv: TEST:JOG:VELO
          command_pv: TEST:JOG:CMD
          command_pos: 1
          command_neg: -1
```

Fields:

| Field | Meaning |
|---|---|
| `type` | Registered plugin type name. |
| `name` | Instance name used for logging. |
| `parameters` | Plugin-specific configuration dictionary. |

## `ScanPlugin` lifecycle

### `__init__(name, parameters=None, scan=None)`

The plugin is created when the scan object is constructed.

The base class stores:

- `self.name`
- `self.parameters`
- `self.logger`
- `self.log_dir`
- `self.scan`

`self.scan` is typed as `ScanPluginContext`, provides not the complete `BaseScan`
implementation. It exposes the following resources:

- `scan.cfg`
- `scan.get_value()` for the last completed point
- `scan.get_current_row_value()` for one value in the point being assembled
- `scan.get_current_row_cache()` for a defensive copy of that point
- `scan.get_actuator()` and `scan.get_actuators()`

For example, a plugin can call `self.scan.get_actuator("energy")`. Plugins
should not depend on other `BaseScan` attributes or private methods.

### `get_headers(timestamps: bool)`

Return the output column names added by the plugin.

The number and order of headers must match the values returned by `get_values()`.

Example:

```python
def get_headers(self, timestamps: bool):
    return ["LatestDrift", "DriftAlarm"]
```

### `get_values(idx, pos)`

Return the plugin values for one scan point.

This method is called during scan acquisition. The returned values are wrapped into the same dictionary format used by detector reads and then appended to the scan row.

Example:

```python
def get_values(self, idx, pos):
    return [self.latest_drift, int(abs(self.latest_drift) > self.limit)]
```

### `on_monitor(ev)`

Optional hook called when a subscription with role `plugin` receives a PV event.

Example YAML:

```yaml
subscriptions:
  - name: drift_feed
    role: plugin
    pv: TEST:DRIFT
```

The event is passed as a `PvEvent` object with fields such as `pvname`, `value`, `timestamp`, `severity`, and `status`.

### Other lifecycle hooks

- `on_start()` runs before point acquisition starts.
- `on_end()` runs during scan cleanup.
- `close()` for releasing plugin-owned resources.

## Timestamp column handling

Plugin output columns are value-only in scan files. The file contains one
common row timestamp, while `include_timestamps: true` adds individual detector
timestamps. 

Return only the headers matching `get_values()`:

```python
def get_headers(self, timestamps: bool):
    del timestamps
    return ["ValueA", "ValueB"]
```


Detector values and earlier plugin results are inserted into the current point
before the next plugin runs. Plugins execute in configuration order, so a later
plugin can read an earlier result.

## Built-in plugins

### `LoggingPlugin`

`LoggingPlugin` adds diagnostic columns to the scan output.

Features:

- optional alarm trace over configured PVs
- optional point-to-point timing measurement
- plugin-specific log file support

Example:

```yaml
plugin_configs:
  - type: LoggingPlugin
    name: scan_log
    parameters:
      log_file: logging_plugin.log
      enable_actuator_trace: True
      enable_point_timing: True
      enable_alarm_trace: True
      alarm_log:
        - TEST:DET:STATUS
        - TEST:MOTOR:STATUS
```

Parameters:

| Parameter | Default | Meaning |
|---|---:|---|
| `log_file` | `logging_plugin.log` | Plugin log filename below the plugin log directory. |
| `log_level` | scan/default logging level | Python logging level. |
| `enable_alarm_trace` | `true` | Add alarm-state columns. |
| `enable_actuator_trace` | `true` | Add actuator columns such as staus cycle count and age.  |
| `enable_point_timing` | `true` | Add point timing column. |
| `alarm_log` | `[]` | List of PV names checked for alarm state. |

Output columns depend on enabled features:

| Feature | Columns |
|---|---|
| alarm trace | `AlarmState`, `AlarmPV`, `AlarmSeverity`, `AlarmStatus` |
| point timing | `PointDtS` |

Route PV events to the plugin, important for the actuator trace feature:
```yaml
# refers to an actuator "m1" defined in actuators section
subscriptions:
  - name: m1_rbv_trace
    role: plugin
    actuator: m1
    source: rbv

  - name: m1_cmd_trace
    role: plugin
    actuator: m1
    source: cmd
    
  - name: m1_status_trace
    role: plugin
    actuator: m1
    source: status
```

### `JogPIDPlugin`

`JogPIDPlugin` is a simple closed-loop controller example.
It reads actuator position and velocity, computes a PID plus velocity-feed-forward setpoint, and calls `jog()` on the configured actuator.

The control calculation runs from `get_values()`, so it is executed at scan points.

Example:

```yaml
plugin_configs:
  - type: JogPIDPlugin
    name: jog_pid
    parameters:
      log_file: jogpid_plugin.txt
      sample_time: 0.5
      kp: 0.001
      ki: 0.0
      kd: 0.0
      kvf: 0.0
      actuator:
        type: epics
        pv: TEST:JOG:SET
        rb_pv: TEST:JOG:RBV
        get_velocity_pv: TEST:JOG:VELO_RBV
        jog:
          velocity_pv: TEST:JOG:VELO
          command_pv: TEST:JOG:CMD
          command_pos: 1
          command_neg: -1
```

Parameters:

| Parameter | Default | Meaning |
|---|---:|---|
| `actuator` | required | Actuator configuration used by the controller. |
| `kp` | `0.001` | Proportional gain or PV name. |
| `ki` | `0.0` | Integral gain or PV name. |
| `kd` | `0.0` | Derivative gain or PV name. |
| `kvf` | `0.0` | Velocity feed-forward gain or PV name. |
| `sample_time` | `1.0` | Minimum time between jog setpoint writes. |
| `log_file` | `jogpid_plugin.txt` | Plugin log filename. |
| `log_level` | scan/default logging level | Python logging level. |

Output column:

```text
ControllerSetpoint
```

No individual timestamp column is added to the scan file.

### TimestampPerformancePlugin

This is a simple plugin convenient for performance measurements. It reads the
raw, representation-neutral detector timestamp keys (`TS-<PV>`) from the
current point cache. It does not require plugin timestamp columns.

Set `include_timestamps: true` only when the detector timestamps should also be saved.

For each configured detector PV `<PV>`, the plugin adds then:

- `PerfDeltaS-<PV>` — current PV timestamp minus its previous timestamp.
- `PerfAgeS-<PV>` — plugin wall-clock time minus the current PV timestamp.

Parameters:

```yaml
include_timestamps: true

plugin_configs:
  - type: TimestampPerformancePlugin
    name: timestamp_performance
```

### `hzb.emc_getter`

Asynchronous plugin that reads scalar values from an HZB EMC (electrometer
controller) over one persistent TCP connection. It runs one background worker,
so the scan thread never waits for the network. Each scan point publishes the
latest complete group of values; until the first group arrives, and after a
failed exchange, the columns contain `NaN` and the error is available from
`get_async_status()`.

Commands are sent one at a time in configuration order, and each reply must
be complete before the next command goes out. A reply has the form
`t <value>`; anything else counts as a failed read. After a failed or
incomplete exchange the connection is closed and opened again on the next job.

Parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `host` | `localhost` | EMC host name or address. |
| `port` | `5001` | EMC TCP port. |
| `timeout_s` | `0.5` | Connect and per-command reply deadline in seconds. |
| `min_interval_s` | `0.0` | Minimum time between two reads; points in between reuse the last values. |
| `reply_terminator` | `none` | `none` treats one TCP receive as one reply (legacy EMC); `lf`, `crlf` or `cr` read until that terminator. |
| `record_times` | `false` | Adds `<column>_before` and `<column>_after` POSIX times around each command. |
| `getters` | required | List of `{command, column, value_type}`; `value_type` is `float` (default) or `int`. |
| `max_workers` | `1` | Must stay `1`; the EMC protocol is serial. |

Example:

```yaml
plugin_configs:
  - type: hzb.emc_getter
    name: emc
    parameters:
      host: emc01
      port: 5001
      timeout_s: 0.5
      reply_terminator: lf
      getters:
        - command: getCurrent1
          column: emc_i1
        - command: getRange1
          column: emc_range1
          value_type: int
```

## Minimal custom plugin example

Create `plugins/drift_watch.py`:

```python
import time
from typing import Dict, Any, List
from kiwi_scan.plugin.registry import register_plugin
from kiwi_scan.plugin.base import ScanPlugin

@register_plugin("DriftWatchPlugin")
class DriftWatchPlugin(ScanPlugin):
    """
    Minimal plugin example:
    - receives (name, parameters, scan) from the plugin factory
    - listens to subscription events with role="plugin"
    - writes two extra columns on every scan point
    """

    def __init__(self, name, parameters=None, scan=None):
        super().__init__(name, parameters or {}, scan)
        self.limit = float(self.parameters.get("limit", 0.03))
        self.latest_drift = None

    def get_headers(self, timestamps: bool):
        del timestamps
        return ["LatestDrift", "DriftAlarm"]

    def get_values(self, idx: int, pos: Dict[str, Any]) -> List[Any]:
        if self.latest_drift is None:
            drift = float("nan")
            alarm = 0
        else:
            drift = self.latest_drift
            alarm = int(abs(drift) > self.limit)
        return [ drift, alarm ]
    
    def on_monitor(self, ev):
        self.logger.debug(f"{ev}")
        try:
            self.actuator = self.scan.get_actuator("energy");
            rbv = self.actuator.rbv
            self.latest_drift = float(ev.value)
            alarm = int(abs(self.latest_drift) > self.limit)
            if alarm and self.actuator.is_ready(): 
                # drift while actuator ready
                self.logger.warning(f"drift={self.latest_drift}, @rbv={rbv}")
        except Exception:
            self.latest_drift = None
```

Enable it:

```bash
export KIWI_SCAN_PLUGIN_PATH="$PWD/plugins"
```

Then run a scan with a subscription that feeds plugin events:

```yaml
subscriptions:
  - name: drift_feed
    role: plugin
    pv: ${IOC_MONO}:DRIFT

plugin_configs:
  - type: DriftWatchPlugin
    name: drift_watch
    parameters:
      limit: 0.03
```

Use this example with `--scan_type linear`, the built-in `LinearScan` dispatches the `plugin` subscription role to `plugin.on_monitor(...)`.
