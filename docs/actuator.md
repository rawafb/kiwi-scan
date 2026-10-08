# Actuator Framework

The interface is defined in the [AbstractActuator](../src/kiwi_scan/actuator/single.py) class. 

Concrete backends implement the real hardware access, for example EPICS PVs or an undulator-specific multi-axis actuator.

The main files of the framework are:

```text
actuator/
  single.py          # common actuator interface and PV event model
  multi.py           # composite actuator for multiple child axes

actuator_concrete/
  single_epics.py    # EPICS implementation of a single actuator
  undulator.py       # two-axis undulator actuator helpers
```

## Purpose

The actuator layer separates scan logic from hardware details. The interface is kept small.

Scans only need a common actuator API:

- move to an absolute position
- move relatively
- jog continuously
- stop motion
- set or read velocity
- read back position
- check ready / moving state
- optionally subscribe to PV monitor events
- read setpoints 

## `actuator/single.py`

`single.py` defines the basic actuator contract.

### `PvEvent`

`PvEvent` is a small data object used for monitor callbacks.  
It stores the PV name, value, optional timestamp, alarm/status fields, source information, and raw backend data.

It is used to decouple scan and plugin logic from raw `pyepics` callback arguments.

### `AbstractActuator`

`AbstractActuator` is the base class for all actuator backends.

Important properties:

| Property | Meaning |
|---|---|
| `pvname` | Primary actuator PV name. |
| `rbv` | Readback value. |
| `cmdv` | Commanded value, if available. |
| `backlash` | Optional backlash compensation distance. |
| `in_position_band` | Position tolerance used after motion. |
| `dwell_time` | Extra wait time after motion completion. |
| `ready_value` | Status value interpreted as ready. |
| `startup_timeout` | Timeout for observing motion start. |

Important methods:

| Method | Meaning |
|---|---|
| `move(position)` | Start an absolute move without waiting. |
| `rel_move(delta)` | Start a relative move without waiting. |
| `run_move(position, sync=True)` | Move and optionally wait until done. |
| `run_rel_move(delta, sync=True)` | Relative move and optionally wait until done. |
| `jog(velocity, sync=True)` | Continuous jog command. |
| `set_velocity(velocity)` | Set actuator velocity. |
| `get_velocity()` | Read current velocity. |
| `is_ready()` | Return `True` when the actuator is ready. |
| `is_moving()` | Default: inverse of `is_ready()`, can be overridden. |
| `is_in_position(target, band)` | Check readback against target. |
| `wait_until_done(position)` | Wait until motion is complete. |
| `stop()` | Stop motion. |

### Monitor support

The base class contains helper methods for monitor-style event handling:

- `supports_monitors()`
- `add_monitor(...)`
- `remove_monitor(...)`
- `clear_monitors()`
- `on_pv_event(...)`
- `get_last_event(...)`

Backends that support monitors, such as the EPICS backend can override these methods.

## `actuator/multi.py`

`MultiActuator` is a composite actuator.

It wraps several child actuators and exposes them as one logical actuator.  
This is useful for devices that must be controlled as a group, for example gap and shift axes.

Behavior:

- `rbv` returns a list of child readbacks.
- `cmdv` returns a list of child commanded values.
- `set_velocity([...])` forwards velocities to all child axes.
- `run_move([...])` starts all child moves.
- `run_rel_move([...])` starts all child relative moves.
- `jog([...])` jogs all child axes.
- `stop()` stops all child axes.
- `is_ready()` is `True` only when all child axes are ready.

Example concept:

```python
multi = MultiActuator([gap_axis, shift_axis], config)
multi.set_velocity([0.1, -0.2])
```

## `actuator_concrete/single_epics.py`

`EpicsActuator` implements `AbstractActuator` using EPICS PVs.

It maps actuator functions to configured PVs:

| Config field | Purpose |
|---|---|
| `pv` | Main absolute move PV. |
| `rel_pv` | Optional relative move PV. |
| `rb_pv` | Readback PV. |
| `cmd_pv` | Commanded position PV. |
| `cmdvel_pv` | Commanded velocity PV. |
| `velocity_pv` | PV used to set velocity. |
| `get_velocity_pv` | PV used to read velocity. |
| `start_pv` | Optional PV written after setting a target. |
| `start_command` | Value written to `start_pv`. |
| `stop_pv` | Stop PV. |
| `stop_command` | Value written to `stop_pv`. |
| `status_pv` | Status PV used for ready/moving checks. |
| `ready_value` | Status value interpreted as ready. |
| `ready_bitmask` | Optional bitmask for status decoding. |
| `in_position_band` | Readback tolerance for final position check. |
| `dwell_time` | Extra wait after motion. |
| `ca_timeout` | EPICS Channel Access timeout. |
| `startup_timeout` | Timeout for observing motion start. |

### Motion behavior

`run_move(position, sync=True)`:

Writes `position` to `pv`, optionally writes `start_command` to `start_pv` and eventually waits for motion completion when `sync=True`.

`run_rel_move(delta, sync=True)`:

Writes `delta` to `rel_pv` if configured, otherwise computes `rbv + delta` and uses the absolute move PV

### Ready / moving state

If `status_pv` is configured, the actuator reads it and compares it with `ready_value`.

With `ready_bitmask != 0`, the ready test is:

```text
(status & ready_bitmask) == ready_value
```

Without a bitmask, a direct value comparison is used.

A temporary unknown status, for example from a short CA disconnect, is not treated as moving as the device status (tri state) but can have the same effect while waiting for ready condition.

### Jog support

Jogging requires a `jog` block in the actuator config with optional jog commands.

```yaml
actuators:
  energy:
    type: epics
    pv: IOC:Energy:Set
    rb_pv: IOC:Energy:RBV
    jog:
      velocity_pv: IOC:Energy:JogVelocity
      command_pv: IOC:Energy:Jog
      command_pos: 1
      command_neg: -1
```

## `actuator_concrete/undulator.py`

`undulator.py` contains two-axis undulator actuator helpers based on `MultiActuator` for gap/shift devices or gap-only devices

### `UndulatorViaEPICS`

Represents an undulator with one or two axes, gap or gap/shift.
```python
undulator = UndulatorViaEPICS(gap_axis, shift_axis|None, config)  # gap (shift==None) or gap/shift
```
The current implementation supports jog-style operation by writing two velocities as a waveform to `jog_velocity_pv`.
If `jog.command_pv` is configured, the start command is written after setting the velocities.

```python
undulator.jog([gap_velocity, shift_velocity])
# or gap only, (fist slot is used for gap):
undulator.jog([gap_velocity, 0.0])
```

### `UndulatorViaCAN`

CAN-specific variant of the undulator actuator.

It packs gap and shift velocity multipliers in the range `0.0` to `1.0` into one 32-bit integer.
Each multiplier is converted into an unsigned 16-bit value (`0x0000` to `0xFFFF`)

```text
packed = (scaled_shift << 16) | scaled_gap
```

The packed value is written to the configured jog command PV.
The shift value bits are set to `0` for a gap-only devices.

Values outside `0.0 .. 1.0` raise `ValueError` instead of being clipped.

### `UndulatorControlCore`

UndulatorControlCore is a hardware-independent PID controller for gap and optional shift axes. 

```
core = UndulatorControlCore(enabled_axes=["gap", "shift"])
results = core.calculate(inputs, gains, now=time.monotonic())
```

Where,

`inputs` and `gains` provide `AxisPidInputs` and `AxisPidGains` for every enabled axis

## Actuator helper API

Construct and monitor actuators:

```python
from kiwi_scan.actuator.factory import create_actuator, create_actuators
from kiwi_scan.actuator.tools import load_actuators, run_monitors
from kiwi_scan.datamodels import MonitorSpec
```

- `create_actuator(config)` creates one actuator from `ActuatorConfig`.
- `create_actuators(configs)` creates a named mapping from `ActuatorConfig`
  objects or raw mappings.
- `load_actuators(config_file, replacements=None)` loads the YAML actuator
  block and creates the mapping.
- `run_monitors(actuators, monitor_specs, ...)` manages monitor subscriptions,
  formatted event output, completion conditions, and cleanup.

## Minimal YAML examples

### Single EPICS actuator

```yaml
actuators:
  energy:
    type: epics
    pv: ${IOC_MONO}:monoSetEnergy
    rb_pv: ${IOC_MONO}:monoGetEnergy
    status_pv: ${IOC_MONO}:Status
    ready_value: 0
    stop_pv: ${IOC_MONO}:Stop
    stop_command: 1
    in_position_band: 0.000001
    dwell_time: 0.05
    ca_timeout: 1.0
    startup_timeout: 0.5
...
```

### Jog-capable actuator

```yaml
actuators:
  slit:
    type: epics
    pv: ${IOC_MONO}:setSlitWidth
    rb_pv: ${IOC_MONO}:getSlitWidth
    status_pv: ${IOC_MONO}:slitStatus
    ready_value: 0
    jog:
      velocity_pv: ${IOC_MONO}:slit:Jog
      command_pv: ${IOC_MONO}:slit:JogCommand
      command_pos: 1
      command_neg: -1
...
```

## `actuator_runner.py`

`actuator_runner` is a command-line tool for direct actuator operation from YAML configuration.

It can:

- move one or more actuators
- perform relative moves
- set velocities
- jog actuators
- stop actuators
- monitor actuator PVs
- write monitor output to a file
- apply `${KEY}` replacements from command line or environment

The tool is intended as a small command-line layer on top of the actuator API.

## Basic `actuator_runner` examples

### Show help

```bash
actuator_runner --help
```

### Absolute move

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --move energy=400
```

### Absolute move with logging

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --move energy=400 \
  --log-level 2
```

### Move two actuators

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --move energy=400 \
  --move slit=0.5
```

### Relative move

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --rel-move energy=1.0
```

### Set velocity and move

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --set-velocity energy=5 \
  --move energy=400
```

### Stop an actuator

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --stop energy
```

### Jog an actuator

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --jog energy=0.2
```

### Monitor readback during a move

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --move energy=400
```

### Monitor readback and status

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --monitor energy:status \
  --move energy=400
```

### Monitor for a fixed duration

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --monitor-duration 10
```

### Monitor a fixed number of events

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --monitor-count 20
```

### Keep monitors alive until Ctrl+C

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --keep-alive
```

### Monitor a direct PV

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy@IOC:Some:PV \
  --monitor-duration 5
```

### Write monitor output to a file

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --monitor energy:rbv \
  --monitor-duration 10 \
  --out actuator_trace.txt
```

### Use a preset config

If `KIWI_SCAN_CONFIG_DIR` points to a directory containing `mono.yaml`:

```bash
actuator_runner \
  --config mono \
  --move energy=400
```

### Use replacements

```bash
actuator_runner \
  --config-file ./mono.yaml \
  --replace IOC_MONO=ue481stxm:mono \
  --move energy=400
```

Environment replacements are also supported:

```bash
export KIWI_SCAN_REPLACE_IOC_MONO=ue481stxm:mono

actuator_runner \
  --config-file ./mono.yaml \
  --move energy=400
```

## Monitor source names

`--monitor NAME:SOURCE` resolves the source from the actuator configuration.

Supported source names:

| Source | Resolved PV |
|---|---|
| `rbv` | `rb_pv`, fallback `pv` |
| `cmd`, `set`, `command` | `cmd_pv`, fallback `pv` |
| `status` | `status_pv` |
| `stop` | `stop_pv` |
| `velocity` | `get_velocity_pv`, `velocity_pv`, `cmdvel_pv`, fallback `pv` |

Examples:

```bash
actuator_runner --config-file ./mono.yaml --monitor energy:rbv --monitor-duration 5
actuator_runner --config-file ./mono.yaml --monitor energy:status --monitor-duration 5
actuator_runner --config-file ./mono.yaml --monitor energy:velocity --monitor-duration 5
```

## Ctrl+C behavior

When interrupted, `actuator_runner` requests shutdown and attempts to stop actuators that were used for motion commands.  
Monitor subscriptions are removed before the tool exits.

## Notes

- `actuator_runner` validates actuator names and malformed `NAME=VALUE` arguments before starting motion.
- Monitor-only mode needs an exit condition: `--monitor-duration`, `--monitor-count`, or `--keep-alive`.
