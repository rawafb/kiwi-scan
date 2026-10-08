
# Kiwi Scan Types

## Supported built-in scan types in `kiwi-scan`:

- **cm** - Continuous monochromator scan using polling or synchronized DAQ and subscriptions.
- **poll** - Poll-based scan reading detector values at fixed intervals or synchronized DAQ and subscriptions.
- **linear** - Standard multi actuator point-by-point linear scan between start and stop positions.
- **approach** - Nonlinear approach-style scan with denser points near the target.
- **para** - Passive parasitical step scan that observes externally moved actuators and records points after they are in range and ready.

Example:

```bash
scan_runner --scan_type linear \
  --config-file config.yaml \
  --dim actuator=energy,start=400,stop=410,steps=11
```
### Parallel and multi-actuator scans

`kiwi-scan` supports scans with more than one actuator. Multiple scan dimensions must be provided e.g. by repeating `--dim` on the command line.

For point-by-point scans, the scan engine builds one position list per actuator. During execution, the positions are aligned to the same number of points. If one actuator has fewer points, its last target is repeated.

This allows scans such as:

```bash
scan_runner \
  --scan_type linear \
  --config-file ./mono.yaml \
  --dim actuator=energy,start=400,stop=410,steps=11 \
  --dim actuator=slit,start=20,stop=25,steps=11
```

### Backlash and overshoot handling

For point-by-point scans, actuators can define `backlash`. If backlash compensation is configured, the scan preparation can add an initial overshoot step before the real scan points. Detector data is skipped for the overshoot point.

### `linear`

`linear` is the standard point-by-point scan type.

It creates evenly spaced positions from `start` to `stop` for each configured actuator. If `steps < 2`, only the start position is used.


#### Stats
`LinearScan` creates one `StatsCollector` for all configured `stat` subscriptions and registers it as a column provider (see stats.md).

That way, `LinearScan` can write simple online statistics for values received through multiple subscriptions.

During a scan point, stat monitor events should happen faster than the detector readout. Instead of writing every stat event, `LinearScan` computes a statistical summary for the current DAQ window (configure `integration_time`).


The following columns are added to the main scan output:

| Column | Meaning |
|---|---|
| `<name>Mean` | Mean value of collected samples |
| `<name>Std` | Standard deviation of collected samples |
| `<name>Min` | Minimum value |
| `<name>Max` | Maximum value |
| `<name>NSamples` | Number of samples collected during the DAQ window |

Example configuration:

```yaml
subscriptions:
  - actuator: energy
    source: rbv
    name: mono_energy
    role: stat
  - pv: "XXX:Beta"
    name: beta
    role: stat
```
### `approach`

`approach` is a nonlinear point-by-point scan type.

It generates a nonlinear series from `start` to `stop` using a non linear progression, producing an approach-style distribution of points.

Typical use cases:

- approach sequences
- testing closed loop systems with different step sizes
- when the target region is more important part of the range

### `para`

`para` is a passive parasitical step-scan type. It does not command actuator motion. Instead, an external process or operator moves the configured actuators, and `para` records one scan point when all configured actuators are inside their configured scan ranges and all of them report ready.

This makes `para` useful as the step-scan counterpart to `poll`: `poll` samples continuously while an actuator is moving, while `para` waits for externally created stable step positions and then runs the normal point-acquisition pipeline.

The scan loop is:

1. wait for an external actuator move to happen
2. wait until all configured actuators are ready
3. check that all readbacks are inside the configured `start`/`stop` ranges
4. run the normal DAQ point pipeline: `on_point` triggers, optional `integration_time`, detector reads, `after_point` triggers, plugins, monitor update, and file writing
5. wait for the next externally initiated movement before accepting another point

The first configured scan dimension is written as the standard `Position` column. For multi-actuator setups, all configured actuator readbacks are checked for range and readiness.

`steps` defines the maximum number of points to record. The scan also terminates when it has already recorded points inside the range and then detects that an actuator has left its configured scan range.

Example:

```bash
scan_runner \
  --scan_type para \
  --config-file ./mono.yaml \
  --dim actuator=energy,start=400,stop=1500,steps=10000
```

Multi-actuator example:

```bash
scan_runner \
  --scan_type para \
  --config-file ./mono.yaml \
  --dim actuator=energy,start=400,stop=410,steps=1000 \
  --dim actuator=theta,start=4.5,stop=5.0,steps=1000
```

Example statistic subscription:

```yaml
subscriptions:
  - pv: "XXX:Beta"
    name: beta
    role: stat
```

As `linear` does, `para` uses the `stat` subscription role for per-point statistics. Values received during the DAQ window are summarized into output columns.

Typical use cases:

- observing a manually or externally driven step scan
- recording detector values only after motion has completed
- passive diagnostics where `kiwi-scan` must not command the actuator
- multi-actuator step scans where every axis must be inside its allowed range before acquisition
- collecting statistics over `integration_time` for externally generated positions

### `poll`

`poll` reads detector values while the primary actuator is moving through a range.

Unlike `linear`, `poll` does not generate a fixed list of move targets for every scan point. Instead, it samples while the first actuator is moving and while the current position is inside the configured start/stop range.

It can use heartbeat and sync subscriptions. Without events, the configured sample time acts as a timeout fallback.

For `poll`, a positive `steps` value is the maximum number of acquired points.
When `steps <= 0`, acquisition continues until motion/range detection or a stop
request ends it.

Typical use cases:

- continuous readout during a move
- diagnostics of an externally started scan
- simple DAQ sampling based on actuator readback
- synchronized acquisition using heartbeat and sync subscriptions
- processing plugin logic

### `cm`

`cm` is a simple implementation of continuous a move scan type.

It moves the configured actuator or actuators to the start position, stores their original velocities, applies the configured scan velocities, starts motion to the stop position, records detector values during the move, and restores the original velocities afterward.

The DAQ loop can be driven by heartbeat subscriptions, with the sample time used as a timeout fallback. The scan position can come from a sync subscription or fall back to the actuator readback.

For `cm`, a positive `steps` value is the maximum number of acquired points.
When `steps <= 0`, the motion, range detector, or a stop request determines the
end of acquisition. Original actuator velocities are restored during cleanup.

Without external `sync` subscriptions, both `cm` and `poll` use fixed absolute
timer slots derived from `sample_rate_hz`. See [sync-controller.md](sync-controller.md).


## External scan-types

External scan types are registered with `register_scan(...)` and discovered from files or directories listed in `KIWI_SCAN_SCAN_PATH`.
New scan classes must be derived from the interface defined in [scan abstraction](../src/kiwi_scan/scan/scan_abs.py)
and should use the documented `ScanABC`/`BaseScan` API.

When a custom scan registers subscription roles, use the event-handler object:

```python
self.register_subscription_role("sync", self.event_handler.on_sync_event)
self.register_subscription_role("status", self.event_handler.on_status_event)
self.register_subscription_role("stop", self.event_handler.on_stop_event)
```

### Custom type example

Create `scan_types/triangle_scan.py`:
```python
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.registry import register_scan


@register_scan("triangle")
class TriangleScan(BaseScan):
    """
    Forward scan, then back again without repeating the end point.
    Example: 0, 1, 2, 1, 0
    """

    def execute(self):
        positions = {}

        for dim in self.scan_dimensions:
            forward = dim.compute_positions_linear()
            backward = list(reversed(forward[:-1]))
            positions[dim.actuator] = forward + backward

        self.scan(positions)
```

Enable it:

```bash
export KIWI_SCAN_SCAN_PATH="$PWD/scan_types"
```

Run it:

```bash
scan_runner \
  --scan_type triangle \
  --config-file mono.yaml \
  --dim actuator=energy,start=400,stop=402,steps=3
```

This produces a trajectory like:

```text
400.0 -> 401.0 -> 402.0 -> 401.0 -> 400.0
```

That pattern is handy for hysteresis checks, warm-up sweeps, and repeatability measurements.
