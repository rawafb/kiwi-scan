# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import os
import queue
import threading

# import pdb
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional

from kiwi_scan.actuator.factory import create_actuator
from kiwi_scan.actuator.single import AbstractActuator, PvEvent
from kiwi_scan.data.loader import DataLoader, resolve_data_dir
from kiwi_scan.data.manifestwriter import ManifestWriter
from kiwi_scan.datamodels import (
    ScanConfig,
    ScanDimension,
    SubscriptionConfig,
)
from kiwi_scan.epics_wrapper import EpicsPV, ensure_ca_context
from kiwi_scan.monitor.base import BaseMonitor
from kiwi_scan.monitor.factory import create_monitor
from kiwi_scan.plugin.registry import create_plugin
from kiwi_scan.scan.scan_abs import ScanABC

from ._point_frame import _DetectorLayout, _PointFrame
from .column_provider import DataColumnProvider
from .detector_reader import DetectorReader, create_detector_reader
from .event_handler import ScanEventHandler
from .metadata_monitor import MetadataCAMonitor
from .output_manager import OutputManager
from .performance_tracker import PerformanceTracker
from .point_pipeline import PointPipeline
from .subscription_manager import SubscriptionManager
from .sync_controller import SyncController
from .trigger_manager import TriggerManager

logger = logging.getLogger(__name__)


class BaseScan(ScanABC):
    """
    A base class for performing scans using EPICS Process Variables (PVs).

    * Event-Driven Scan Architecture *

            [Triggers]        [Plugins]
             ↑               ↓↑
         ┌────────────────────────┐
         │      Scan Engine       │
         └────────────────────────┘
           ↑          ↑↓         ↓
   [Subscriptions]←[Actuators] [Data Writer]
           ↑                      ↓
       [Monitors]        [Metadata Sidecar]
    """
    def __init__(self, config: ScanConfig, data_dir=None):
        ensure_ca_context()
        super().__init__(config, data_dir)
        logger.debug("Init BaseScan")

        self._initialize_config(config)
        self._initialize_data_output(data_dir)
        self._initialize_scan_timing()
        self._connect_detectors()
        self._connect_actuators()
        self._initialize_runtime_managers()
        self._initialize_stop_pv()
        self._initialize_metadata_monitor()
        self._initialize_performance_tracker()
        self._initialize_runtime_state()
        self._initialize_event_state()
        self._start_event_workers()
        self._initialize_event_handler()
        self._start_legacy_subscriptions_if_needed()

    def _initialize_config(self, config: ScanConfig) -> None:
        """Validate and normalize scan configuration used during runtime."""
        self.busyflag = False
        self.cfg = config
        self.cfg.validate()
        self.scan_type = self.__class__.__name__
        self._validate_and_filter_actuators()

        self.plugins = [
            create_plugin(plugin_config, self)
            for plugin_config in self.cfg.plugin_configs
        ]
        logger.debug("Plugin Configs: %s", self.cfg.plugin_configs)

        self.scan_dimensions = config.scan_dimensions or []
        self.parallel_scans = config.parallel_scans or []
        self.nested_scans = config.nested_scans or []
        self.trigger_manager = TriggerManager.from_config(self.cfg.triggers)

    def _initialize_data_output(self, data_dir) -> None:
        """Create the main scan-file lifecycle manager."""
        self.data_dir = os.path.abspath(
            resolve_data_dir(data_dir, self.cfg.data_dir)
        )
        logger.info("Data directory: %s", self.data_dir)

        self.output_manager = OutputManager(
            data_dir=self.data_dir,
            requested_output_file=self.cfg.output_file,
            data_writing_enabled=self.cfg.data_writing_enabled,
        )
        self.output_manager.ensure_output_file_exists()
        self._manifest_mode = self.cfg.manifest_mode

    def _initialize_scan_timing(self) -> None:
        """Initialize timestamp and sample-rate settings."""
        self.include_timestamps = self.cfg.include_timestamps
        self.timestamp_output_format = self.cfg.timestamp_output_format
        self.sample_rate_hz = 1.0
        self.sampletime = 1.0
        self._apply_sample_rate(self.cfg.sample_rate_hz)
        self.debug = self.cfg.debug

    def _initialize_runtime_managers(self) -> None:
        """Create subscription and synchronization managers."""
        logger.debug("init subscription manager")

        subscriptions = self.cfg.subscriptions
        self.subscription_manager = SubscriptionManager(
            subscriptions,
            actuator_configs=self.cfg.actuators,
            actuators=self.actuators,
        )
        self.sync_controller = SyncController(subscriptions)
        self._validate_config()

    def _initialize_stop_pv(self) -> None:
        """Connect the optional external stop process variable."""
        if self.cfg.stop_pv:
            self.stop_pv = EpicsPV(self.cfg.stop_pv)
            self.prefix = self.cfg.stop_pv.split(":")[0]
        else:
            self.stop_pv = None

    def _initialize_metadata_monitor(self) -> None:
        """Prepare the metadata sidecar monitor without starting it."""
        base_name, ext = os.path.splitext(
            self.cfg.metadata_file or "scan_metadata.txt"
        )
        self._metadata_out = os.path.join(
            self.data_dir,
            f"{base_name}-{self.output_manager.output_timestamp}{ext}",
        )
        self._meta_mon = MetadataCAMonitor(
            pvs=list(self.cfg.metadata_pvs or []),
            constants=dict(self.cfg.metadata_constants or {}),
            outfile=self._metadata_out,
            queue_maxsize=20000,
        )
        self._meta_mon_started = False

    def _initialize_runtime_state(self) -> None:
        """Initialize mutable per-scan runtime state."""
        self._position: Any = None
        self._initialize_point_pipeline()
        self._daq_is_on = False
        self.integration_time = self.cfg.integration_time

    def _initialize_point_pipeline(self) -> None:
        """ Create the point/cache/column subsystem with performance callbacks. """
        detector_layout = _DetectorLayout.from_headers(
            pv.pvname for pv in self.detector_pvs
        )
        self._point_pipeline = PointPipeline(
            include_timestamps=self.include_timestamps,
            timestamp_output_format=self.timestamp_output_format,
            detector_layout=detector_layout,
            get_plugins=lambda: tuple(self.plugins),
            performance_enabled=lambda: self.performance.enabled,
            record_perf_sample=self.performance.record_sample,
            writer_queue_size=1024,    # TODO: optimal size
        )
        self.output_manager.set_header_factory(self._point_pipeline.build_output_headers)

    def _get_writer_metrics(self) -> tuple[int, float]:
        return (
            self._point_pipeline.point_writer_queue_high_water,
            self._point_pipeline.point_writer_maximum_queue_delay,
        )

    def _initialize_performance_tracker(self) -> None:
        """Create the independent scan performance/diagnostics subsystem."""
        self.performance = PerformanceTracker(
            enabled=bool(self.cfg.debug or self.cfg.performance_report),
            metadata_queue_drop_count=self.get_metadata_queue_drop_count,
            writer_metrics=self._get_writer_metrics,
        )

    def _initialize_event_state(self) -> None:
        """Initialize event-driven synchronization state."""
        self._tick_cond = threading.Condition()
        self._tick_seq = 0
        self._stop_requested = threading.Event()
        self._last_heartbeat: Optional[PvEvent] = None
        self._last_status: Optional[PvEvent] = None
        self._position_sync_subscription_set = False

    def _start_event_workers(self) -> None:
        """Start worker threads used outside EPICS callback context."""
        self._trigger_q = queue.SimpleQueue()
        self._trigger_worker_stop = threading.Event()
        self._trigger_worker = threading.Thread(
            target=self._trigger_worker_loop,
            daemon=True,
        )
        self._trigger_worker.start()

        self._plugin_q = queue.SimpleQueue()
        self._plugin_worker_stop = threading.Event()
        self._plugin_worker = threading.Thread(
            target=self._plugin_worker_loop,
            daemon=True,
        )
        self._plugin_worker.start()

    def _initialize_event_handler(self) -> None:
        """Create default subscription handlers after event queues exist."""
        self.event_handler = ScanEventHandler(self)

    def _start_legacy_subscriptions_if_needed(self) -> None:
        """Preserve automatic subscription startup for legacy scan types."""
        if getattr(self, "ROLE_CALLBACKS", None):
            logger.warning("Detected legacy ROLE_CALLBACKS on %s; starting subscriptions. This feature will be removed soon.", 
                           type(self).__name__)
            self._start_subscriptions()

    # -------------------- subscription/callback integration --------------------

    def register_subscription_role(
        self,
        role: str,
        handler: Callable[[PvEvent, SubscriptionConfig], None],
    ) -> None:
        self.subscription_manager.register_role(role, handler)

    def _start_subscriptions(self) -> None:
        self.subscription_manager.start()

    def _stop_subscriptions(self) -> None:
        self.subscription_manager.stop()

    def _clear_subscriptions(self) -> None:
        self._stop_subscriptions()

    def _fire_triggers(self, phase: str) -> None:
        """Compatibility wrapper delegating trigger execution to TriggerManager."""
        self.trigger_manager.fire(phase)

    def _primary_actuator_name(self):
        if not self.scan_dimensions:
            return None
        return self.scan_dimensions[0].actuator

    def _is_position_sync_subscription(self, subscription) -> bool:
        """
        When multiple sync subscriptions exist, only one should update
        self._position. For now, use the primary actuator RBV sync source.
        """
        if subscription is None:
            return True

        primary_name = self._primary_actuator_name()
        if primary_name is None:
            return True

        if getattr(subscription, "actuator", None) == primary_name:
            source = (getattr(subscription, "source", None) or "rbv").lower()
            return source == "rbv"

        return False

    def _arm_sync_controller(self) -> None:
        self.sync_controller.arm()

    def _wait_for_sync(self, stop_event=None) -> bool:
        ok = self.sync_controller.wait(stop_event=stop_event)
        if not ok:
            logger.debug("SyncController wait aborted (required=%s)", list(self.sync_controller.required_names))
        return ok

    def _validate_and_filter_actuators(self):
        if not self.cfg.scan_dimensions:
            raise ValueError("ScanConfig must contain at least one ScanDimension")

        # Extract actuator names from scan_dimensions
        dim_actuators = {dim.actuator for dim in self.cfg.scan_dimensions}

        # Filter out actuators not referenced in dimensions
        all_actuators = set(self.cfg.actuators.keys())
        unused_actuators = all_actuators - dim_actuators

        if unused_actuators:
            logger.warning(f"Removing unused actuators not referenced in scan_dimensions: {unused_actuators}")
            for name in unused_actuators:
                del self.cfg.actuators[name]

        # Ensure every dimension references a valid actuator
        for dim in self.cfg.scan_dimensions:
            if dim.actuator not in self.cfg.actuators:
                raise ValueError(f"ScanDimension refers to unknown actuator: '{dim.actuator}'")

    def _connect_detectors(self):
        logger.debug(f"Detector PVs: {self.cfg.detector_pvs}")
        logger.debug("Init Detectors")
        logger.debug(f"Monitor: {self.cfg.detector_pvs_monitor}")

        self.detector_pvs = []
        self.detector_pvs_monitor = self.cfg.detector_pvs_monitor
        for i, pvname in enumerate(self.cfg.detector_pvs):
            logger.debug("Creating detector PV %d/%d: %s", i+1, len(self.cfg.detector_pvs), pvname)
            pv = EpicsPV(
                pvname,
                timeout=1.0,
                connection_timeout=1.0,
                queueing_delay=0.0,
                auto_monitor=True,
            )
            logger.debug("Created detector PV: %s", pvname)
            self.detector_pvs.append(pv)
        logger.info("PVs connected: %d", len(self.detector_pvs))
        self._detector_reader: DetectorReader = create_detector_reader(
            self.cfg.detector_reader_strategy,
            self.detector_pvs,
            use_monitor=self.detector_pvs_monitor,
        )

    def _connect_actuators(self) -> None:
        """ Connect actuators from the already validated scan configuration. """
        logger.debug("Init Actuators")

        actuators: Dict[str, AbstractActuator] = {}
        for name, cfg in self.cfg.actuators.items():
            logger.info("Creating actuator '%s' → PV='%s', RB_PV='%s'", name, cfg.pv, cfg.rb_pv)
            actuators[name] = create_actuator(cfg)
            logger.debug("Actuator created")

        self.actuators = actuators
        logger.info("Number of actuators: %d", len(self.actuators))

    def _validate_config(self):
        if not (self.scan_dimensions or self.parallel_scans or self.nested_scans):
            raise ValueError("No scan dimensions provided in ScanConfig.")

    def _apply_sample_rate(self, sample_rate_hz: Optional[float]) -> None:
        """Store the current scan sample rate and derived sample period."""
        if sample_rate_hz is None:
            sample_rate_hz = 1.0

        rate = float(sample_rate_hz)
        if rate <= 0.0:
            logger.error(f"sample_rate_hz must be positive, got {rate}")
            rate = rate * -1.0

        self.sample_rate_hz = rate
        self.sampletime = 1.0 / rate

    def set_samplerate( self, dim: Optional[ScanDimension] = None, sample_rate_hz: Optional[float] = None):
        """ Set the scan sample rate.  """

        rate_hz = sample_rate_hz
        if rate_hz is None:
            rate_hz = self.cfg.sample_rate_hz
        self._apply_sample_rate(rate_hz)
        self.sync_controller.set_timer_period(self.sampletime)
    
    @property
    def output_file(self) -> Optional[str]:
        """Current main scan data-file path, backed by OutputManager."""
        return self.output_manager.output_file

    @output_file.setter
    def output_file(self, path: Optional[str]) -> None:
        self.output_manager.output_file = path

    def get_data_writing_enabled(self) -> bool:
        """Return whether main scan data and metadata writing are enabled."""
        return self.output_manager.get_data_writing_enabled()

    def set_data_writing_enabled(self, enabled: bool) -> None:
        """
        Enable/disable scan data and metadata writing at runtime.

        This method remains part of the public scan API used by the scan IOC.
        OutputManager owns the writing flag; BaseScan owns metadata-monitor
        orchestration triggered by changes to that flag.
        """
        enabled = bool(enabled)
        changed = self.output_manager.set_data_writing_enabled(enabled)
        if not changed:
            return

        if enabled:
            logger.info("Data writing enabled")
            if self.busy:
                self._start_metadata_monitor()
        else:
            logger.info("Data writing disabled")
            self._stop_metadata_monitor()

    def get_output_file(self) -> Optional[str]:
        """Return the current main scan output-file path."""
        return self.output_manager.output_file

    def _start_metadata_monitor(self) -> None:
        if not self.get_data_writing_enabled():
            logger.info("Metadata monitor not started: data writing is disabled")
            return
        if self._meta_mon_started:
            return
        try:
            self._meta_mon.start()
            self._meta_mon_started = True
            logger.info("Started metadata task")
        except Exception:
            logger.exception("Failed to start metadata monitor")

    def _stop_metadata_monitor(self) -> None:
        if not self._meta_mon_started:
            return
        try:
            self._meta_mon.stop()
        except Exception:
            logger.exception("Error stopping metadata monitor")
        finally:
            self._meta_mon_started = False

    def get_metadata_queue_drop_count(self) -> int:
        """ Return metadata monitor queue drops for diagnostics/performance reports. """
        monitor: Optional[MetadataCAMonitor] = getattr(self, "_meta_mon", None)   # maybe no meta monitor at all
        if monitor is None:
            return 0

        try:
            return monitor.get_drop_count()
        except Exception:
            logger.debug("Failed to read metadata monitor drop count", exc_info=True)
            return 0

    def read_detectors(self) -> List[Any]:
        """Return one ordered reading per configured detector PV."""
        return self._detector_reader.read()

    def _start_detector_reader(self) -> None:
        self._detector_reader.start()

    def _stop_detector_reader(self) -> None:  # nice for cleanup steps
        self._detector_reader.stop()

    # -------------------- data column provider integration --------------------

    def add_column_provider(self, provider: DataColumnProvider) -> None:
        """Compatibility façade for PointPipeline provider registration."""
        self._point_pipeline.add_column_provider(provider)

    def _begin_point_frame(
        self,
        *,
        idx: int,
        pos: Any,
        values: List[Any],
        headers: Optional[List[str]] = None,
        provider_values: Optional[List[Any]] = None,
        line_timestamp: Optional[float] = None,
        clear: bool = True,
    ) -> _PointFrame:
        """Compatibility façade for the point pipeline hot path."""
        return self._point_pipeline.begin_point_frame(
            idx=idx,
            pos=pos,
            values=values,
            headers=headers,
            provider_values=provider_values,
            line_timestamp=line_timestamp,
            clear=clear,
        )
    
    

    def get_current_row_cache(self) -> Dict[str, Any]:
        """Return a copy of the current scan-line cache."""
        return self._point_pipeline.get_current_row_cache()

    def get_current_row_value(self, key: str, default: Any = None) -> Any:
        """Return one scalar value from the current scan-line cache."""
        return self._point_pipeline.get_current_row_value(key, default)

    def _commit_point_async(
        self,
        position: Any,
        detector_values: List[Any],
        include_timestamps: Optional[bool] = None,
    ) -> List[Any]:
        """Publish a point using the output policy fixed at initialization."""
        del include_timestamps  # retained only for legacy/custom-scan compatibility
        point = self._point_pipeline.prepare_internal_point(
            position,
            detector_values,
            self._point_pipeline.include_timestamps,
        )
        row_values = list(point.row_values)

        with self.output_manager.point_write() as output_file:
            if output_file is None:
                logger.debug("Skipping data write because data writing is disabled")
                return row_values

            self._point_pipeline.persist_prepared_point_async(
                output_file,
                point,
            )

        return row_values

    def save_to_file(self, position, detector_values, include_timestamps=True):
        """
        Synchronously write one scan row and return its concrete values.

        Compatibility note: when a scan's parallel writer is already active,
        this method enters the same FIFO and waits for its own request. Thus
        existing callers retain immediate write-error and ordering semantics.
        """
        logger.debug("Detector values to be written: %s", detector_values)
        del include_timestamps  # output policy is fixed when PointPipeline is created
        point = self._point_pipeline.prepare_legacy_point(
            position,
            detector_values,
            self._point_pipeline.include_timestamps,
        )
        row_values = list(point.row_values)

        with self.output_manager.point_write() as output_file:
            if output_file is None:
                logger.debug("Skipping data write because data writing is disabled")
                return row_values

            self._point_pipeline.persist_prepared_point_sync(
                output_file,
                point,
            )

        return row_values

    def get_value(
        self,
        name: str,
        *,
        default: Any = None,
        with_metadata: bool = False,
    ) -> Any:
        """Return the last-acquired datapoint by column name."""
        return self._point_pipeline.get_value(name, default=default, with_metadata=with_metadata)

    def get_last_point_keys(self) -> List[str]:
        """Return the currently available keys for get_value()."""
        return self._point_pipeline.get_last_point_keys()

    def load_data(self):
        """
        Load recent data file
        """
        if self.output_file is None:
            logger.info("No scan data file exists")
            return None
        data_loader = DataLoader(self.output_file, data_dir=self.data_dir)
        return data_loader.load_data()

    def write_header_to_output_file(self) -> None:
        """Build the scan header and delegate file lifecycle to OutputManager."""
        if not self.get_data_writing_enabled():
            logger.debug("Skipping header write because data writing is disabled")
            return
        if self.output_manager.header_written:
            return

        headers = self._point_pipeline.build_output_headers()
        self.output_manager.write_header(headers)

    def get_stop_pv(self):
        """ 
        Read stop PV and reset it if triggered (value == 1).
        Returns the current PV value or None on failure.  
        """
        value = None
        if self.stop_pv:
            try:
                value = self.stop_pv.get()
                logger.info("Scan stop PV value received: %s", value)
            except Exception as e: # noqa: BLE001
                logger.error( "Failed to get stop PV %s: %s", self.stop_pv.pvname, e)
            if value == 1:
                try:
                    self.stop_pv.put(0)
                except Exception as e: # noqa: BLE001
                    logger.error( "Failed to reset stop PV %s: %s", self.stop_pv.pvname, e)
        return value
    
    def _daq_stop_requested(self) -> bool:
        """Return whether an event or the configured stop PV ended DAQ."""
        if self._stop_requested.is_set():
            logger.debug("Stop event set")
            return True
        stop_pv_value = self.get_stop_pv()
        if stop_pv_value != 1:
            return False
        logger.debug("Stop PV set")
        self.stop()
        return True

    def _wait_for_scan_cycle(self, index) -> bool:
        """Arm and wait for one synchronized acquisition cycle."""
        self._arm_sync_controller()
        if self._stop_requested.is_set():
            logger.debug("Stop event set")
            return False

        with self.performance.time_block("sync:wait", idx=index):
            self._wait_for_sync(stop_event=self._stop_requested)
        if self._stop_requested.is_set():
            logger.debug("Stop event set")
            return False
        return True

    def _acquire_daq_point_continuous(self, index: int, position, monitor: Optional[BaseMonitor] = None) -> None:
        """Acquire, process, persist, and publish one continuous-motion point."""
        with self.performance.time_block("daq:point", idx=index):
            with self.performance.time_block("triggers:on_point", idx=index):
                self._fire_triggers("on_point")
            with self.performance.time_block("read_detectors", idx=index):
                values = self.read_detectors()
            with self.performance.time_block("update_row_cache", idx=index):
                self._begin_point_frame(idx=index, pos=position, values=values)
            with self.performance.time_block("triggers:after_point", idx=index):
                self._fire_triggers("after_point")
            with self.performance.time_block("plugins", idx=index):
                self._collect_plugin_point_data(index, position)
            with self.performance.time_block("write:data", idx=index):
                monitor_values = self._commit_point_async(position, values)
            with self.performance.time_block("monitor:update", idx=index):
                if monitor is not None:
                    logger.debug("Monitor values: %s", monitor_values)
                    monitor.update(monitor_values)

    def _start_plugins(self) -> None:
        """Run plugin start hooks."""
        for plugin in self.plugins:
            try:
                logger.debug("Starting plugin %s", plugin.name)
                plugin.on_start()
            except Exception:
                logger.exception("Failed to start plugin %s", plugin.name)
    
    def _end_plugins(self) -> None:
        """Run plugin end hooks."""
        for plugin in self.plugins:
            try:
                logger.debug("Ending plugin %s", plugin.name)
                plugin.on_end()
            except Exception:
                logger.exception("Failed to end plugin %s", plugin.name)

    def _close_plugins(self) -> None:
        """Close plugin-owned resources."""
        for plugin in self.plugins:
            try:
                logger.debug("Closing plugin %s", plugin.name)
                plugin.close()
            except Exception:
                logger.exception("Failed to close plugin %s", plugin.name)

    def _run_cleanup_step(
        self,
        label: str,
        cleanup: Callable[[], Any],
    ) -> None:
        """Run one cleanup operation without preventing later cleanup steps."""
        try:
            with self.performance.time_block(label):
                cleanup()
        except Exception:
            logger.exception("Error during scan cleanup step '%s'", label)

    def _drain_parallel_writer_for_cleanup(self) -> Optional[BaseException]:
        """Drain accepted rows while allowing all later cleanup to run."""
        try:
            with self.performance.time_block("write:drain"), \
                    self.output_manager.locked():
                self._point_pipeline.stop_parallel_writer()
        except BaseException as exc:  # noqa: BLE001
            return exc
        return None

    @staticmethod
    def _propagate_parallel_writer_error(
        writer_error: Optional[BaseException],
        *,
        scan_failed: bool,
    ) -> None:
        """Raise a writer error unless another scan error is already active."""
        if writer_error is None:
            return
        if scan_failed:
            logger.error(
                "Point writer also failed while propagating scan error",
                exc_info=(
                    type(writer_error),
                    writer_error,
                    writer_error.__traceback__,
                ),
            )
            return
        raise writer_error

    def _collect_plugin_point_data(
        self,
        index: int,
        position: Any,
    ) -> List[Any]:
        """Run point plugins and expose each result to subsequent plugins."""
        plugin_values: List[Any] = []
        try:
            for plugin in self.plugins:
                data = plugin.on_scan_point(index, position)
                plugin_values.extend(data)
                self._point_pipeline.append_plugin_point_values(
                    plugin.get_headers(False),
                    data,
                )
        except Exception:
            self._point_pipeline.abort_active_point_frame()
            raise
        return plugin_values

    def _move_scan_step(
        self,
        positions: Dict[str, List[Any]],
        index: int,
    ) -> bool:
        """Issue all actuator moves for one scan step.

        Return ``False`` if a stop was requested before all moves were issued.
        """
        self._daq_is_on = False
        for name, actuator in self.actuators.items():
            if name not in positions:
                continue

            target = positions[name][index]
            if self._stop_requested.is_set():
                logger.info("Stop requested—skipping remaining move commands.")
                return False

            logger.info("[%s] moving to %s", name, target)
            actuator.move(target)

        return not self._stop_requested.is_set()

    def _acquire_daq_point_step(self, index: int, position: Any, monitor: Optional[BaseMonitor]) -> bool:
        """
        Acquire, process, save, and publish one step scan point.
        Return ``False`` if acquisition was interrupted during integration.
        """
        self._point_pipeline.reset_data_column_provider_windows()
        self._daq_is_on = True
        with self.performance.time_block("triggers:on_point", idx=index):
            self._fire_triggers("on_point")
        if self.integration_time > 0.0:
            logger.info("DAQ for integration_time = %s", self.integration_time)
            if self._stop_requested.wait(self.integration_time):
                logger.info("Stop requested during integration time")
                return False
        else:
            logger.info("integration_time = %s", self.integration_time)
        with self.performance.time_block("read_detectors", idx=index):
            values = self.read_detectors()
        with self.performance.time_block("update_row_cache", idx=index):
            self._begin_point_frame(idx=index, pos=position, values=values)
        with self.performance.time_block("plugins", idx=index):
            self._collect_plugin_point_data(index, position)
        with self.performance.time_block("write:data", idx=index):
            monitor_values = self._commit_point_async(position, values)
        self._position = position
        with self.performance.time_block("monitor:update", idx=index):
            if monitor is not None:
                logger.debug("Monitor values: %s", monitor_values)
                monitor.update(monitor_values)
        return True

    def _start_scan_services(self, positions) -> None:
        """Initialize services required by a scan."""
        self.write_header_to_output_file()
        self._start_detector_reader()
        self._start_plugins()
        self._start_subscriptions()
        logger.debug("Actuators: %s, positions: %s", list(self.actuators), positions)
        self._start_metadata_monitor()

    def scan(self, positions, monitor: Optional[BaseMonitor] = None):
        """
        Parallel multi-actuator scan:
         1. pad all position lists to equal length
         2. optionally prepend an overshoot point (if any backlash>0)
         3. broadcast moves, wait in parallel, then read & save (skipping the overshoot)
        """
        ensure_ca_context()
        self.busyflag = True
        self._stop_requested.clear()
        scan_failed = False
        try:
            self._start_scan_services(positions)
            # prepare new_positions and tell us if we added an overshoot step
            new_positions, overshoot_applied = self._prepare_positions(positions)
            if not new_positions:
                logger.warning("No valid actuators with positions—nothing to scan.")
                self.busyflag = False
                return

            # how many total steps (includes overshoot if applied)
            step_count = len(next(iter(new_positions.values())))
            first_actuator = next(iter(new_positions))

            self._fire_triggers("before")
            for index in range(step_count):
                if self._stop_requested.is_set():
                    logger.info("Stop requested—aborting scan before step %d.", index)
                    break

                if not self._move_scan_step(new_positions, index):
                    break

                # 2) wait for all in parallel
                self._parallel_wait(
                    {name: self.actuators[name] for name in new_positions},
                    {name: new_positions[name][index] for name in new_positions}
                )

                if self._stop_requested.is_set():
                    logger.info("Stop requested—aborting scan after actuator wait.")
                    break

                # 3) skip detector‐read on the overshoot step
                if overshoot_applied and index == 0:
                    continue

                # 4) read detectors & save & monitor
                position = new_positions[first_actuator][index]
                if not self._acquire_daq_point_step(index, position, monitor):
                    break

                # 5) abort if needed
                if self.get_stop_pv() == 1:
                    logger.info("Stop PV triggered—aborting scan.")
                    break

            self._fire_triggers("after")
            logger.info("Scan complete for all actuators.")

        except BaseException:
            scan_failed = True
            raise
        finally:
            self._daq_is_on = False
            writer_error = self._drain_parallel_writer_for_cleanup()
            self._run_cleanup_step(
                "detectors:stop",
                self._stop_detector_reader,
            )
            self._run_cleanup_step("plugins:stop", self._end_plugins)
            self._run_cleanup_step("plugins:close", self._close_plugins)
            self._run_cleanup_step("metadata:stop", self._stop_metadata_monitor)
            self._run_cleanup_step("subscriptions:stop", self._stop_subscriptions)
            if monitor is not None:
                self._run_cleanup_step("monitor:close", monitor.close)
            self.busyflag = False
            self._run_cleanup_step("performance:report", self.performance.report)
            self._propagate_parallel_writer_error(
                writer_error,
                scan_failed=scan_failed,
            )

    def _execute_standard(self, positions):
        if self.get_data_writing_enabled():
            with self.performance.time_block("manifest:append"):
                self.append_to_manifest()
        else:
            logger.debug("Data writer disabled, not added to manifest")

        monitor = create_monitor(self.cfg)
        monitor_headers = self._point_pipeline.build_output_headers()
        if monitor is not None:
            logger.debug("Starting monitor")
            monitor.start(monitor_headers, headers=monitor_headers)

        scan_errors: List[Exception] = []

        def _run_scan() -> None:
            ensure_ca_context()
            try:
                self.scan(positions, monitor)
            except Exception as exc: # noqa: BLE001
                scan_errors.append(exc)
        scan_thread = threading.Thread(target=_run_scan, name=f"{self.__class__.__name__}-worker")
        logger.info(f"Starting {self.__class__.__name__}.")
        scan_thread.start()
        if monitor is not None:
            monitor.loop()
        scan_thread.join()

        if scan_errors:
            # Pass failures to upper layer. 
            raise scan_errors[0]
        logger.info(f"{self.__class__.__name__} scan complete.")

    def _prepare_positions(self, positions):
        """
        1) Pad each actuator’s position list by repeating its last element
           so all have the same length.
        2) If any actuator has non-zero backlash *inject* an initial overshoot
           for each axis (otherwise leave lists as is).

        Returns:
          - new_positions: dict[name → list of targets]
          - overshoot_applied: bool
        """
        # filter out actuators with no positions
        filtered = {
            name: pts[:]  # copy
            for name, pts in positions.items()
            if pts
        }

        # nothing to do?
        if not filtered:
            return {}, False

        # pad to max length
        max_len = max(len(pts) for pts in filtered.values())
        for name, pts in filtered.items():
            if len(pts) < max_len:
                pts.extend([pts[-1]] * (max_len - len(pts)))

        # check if any backlash
        any_backlash = any(
            self.actuators[name].backlash != 0.0
            for name in filtered
            if name in self.actuators
        )

        if not any_backlash:
            # no overshoot step needed
            return filtered, False

        # build overshoot + real sequences
        prepared = {}
        for name, pts in filtered.items():
            act = self.actuators.get(name)
            if not act:
                continue
            first, second = pts[0], pts[1] if len(pts) > 1 else pts[0]
            if act.backlash != 0.0 and len(pts) > 1:
                bdist = -act.backlash if second > first else act.backlash
                overshoot = first + bdist
            else:
                # duplicate first point
                overshoot = first
            prepared[name] = [overshoot] + pts

        return prepared, True

    def _parallel_wait(self, acts: dict, targets: dict):
        """
        Wait for multiple actuators to reach their target positions in parallel.

        This method starts a separate thread for each actuator's `wait_until_done(target)`
        method, allowing all actuators to be monitored for completion concurrently.
        This prevents blocking on one actuator's wait and allows for more responsive
        overall execution, especially when actuators finish at different times.

        Parameters
        ----------
        acts : dict
            Dictionary mapping actuator names (str) to actuator objects.
            Each actuator must implement a `wait_until_done(target)` method.
        targets : dict
            Dictionary mapping actuator names (str) to target positions/values.

        Notes
        -----
        If any actuator's `wait_until_done` method raises an exception, it will be
        caught and logged with the actuator's name. All waits are attempted,
        even if some fail.

        Example
        -------
        >>> self._parallel_wait(
                acts={'motor1': m1, 'motor2': m2},
                targets={'motor1': 10.0, 'motor2': 5.0}
            )
        """
        def _wait_one(name, act):
            try:
                return act.wait_until_done(targets[name], self._stop_requested)
            except TypeError:
                return act.wait_until_done(targets[name])

        with ThreadPoolExecutor(max_workers=len(acts)) as exe:
            futures = {
                exe.submit(_wait_one, name, act): name
                for name, act in acts.items()
            }

            pending = set(futures)
            while pending:
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )

                for fut in done:
                    name = futures[fut]
                    exc = fut.exception()

                    if exc is not None:
                        logger.error("[%s] actuator wait failed: %s", name, exc)

                if self._stop_requested.is_set():
                    logger.info("Stop requested—waiting for actuator wait workers to exit.")

    def stop(self) -> None:
        """Request scan stop and best-effort stop all configured actuators."""
        logger.info("Stop requested for %s", self.__class__.__name__)

        try:
            self._stop_requested.set()
        except Exception:
            logger.debug("Failed to set scan stop event", exc_info=True)

        self._daq_is_on = False

        for name, actuator in self.actuators.items():
            try:
                logger.info("Stopping actuator '%s'", name)
                actuator.stop()
            except Exception:
                logger.exception("Failed to stop actuator '%s'", name)

        try:
            self.sync_controller.wake()
            with self._tick_cond:
                self._tick_seq += 1
                self._tick_cond.notify_all()
        except Exception:
            logger.debug("Failed to wake scan wait condition", exc_info=True)

    @property
    def busy(self) -> bool:
        """True while scan"""
        return self.busyflag
    
    @property
    def position(self) ->  Any:
        return self._position

    def get_actuator(self, name: str) -> AbstractActuator:
        """
        Return the actuator object by name.

        This is intended for sharing actuators between scans/plugins or for
        composition in higher-level scan engines.
        """
        if not hasattr(self, "actuators") or self.actuators is None:
            raise RuntimeError("Actuators are not initialized on this scan instance.")

        try:
            act = self.actuators[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.actuators.keys()))
            raise KeyError(
                f"Unknown actuator '{name}'. Available actuators: {available}"
            ) from exc

        if act is None:
            raise KeyError(f"Actuator '{name}' exists but is None.")

        return act

    def get_actuators(self) -> Dict[str, AbstractActuator]:
        """
        Return the full actuator mapping (name -> actuator object).

        Returns a shallow copy to prevent accidental mutation of internal state.
        """
        if not hasattr(self, "actuators") or self.actuators is None:
            return {}
        return dict(self.actuators)

    def append_to_manifest(self, scan_type: str | None = None) -> None:
        """
        Append scan configuration to the active manifest.
        Args:
            scan_type: Optional explicit scan type (preferred over class name)
            metadata: Optional extra metadata dict
        """
        try:
            writer = ManifestWriter.from_active()
            if writer is None:
                return

            writer.append_scan_config(
                config=self.cfg,
                scan_type=scan_type or getattr(self, "scan_type", self.__class__.__name__),
                path=self.data_dir,
                data_file=self.output_file,
                metadata_file=self._metadata_out,
                mode=self._manifest_mode 
            )

        except Exception:
            logger.exception("Failed to append scan to manifest")
    
    # -------------------- event wait / worker runtime --------------------

    def _wait_for_tick_or_timeout(self, timeout_s: float) -> bool:
        """
        Helper used with heartbeat/status subscription event handlers
        Wait until:
          - a heartbeat tick arrives (returns True), or
          - timeout occurs (returns False), or
          - stop is requested (returns False).
        Example usage in scan thread or plugin threads:
                yaml: # configure which event uses role="heartbeat"
                    subscriptions:
                      - name: daq_heartbeat
                        role: heartbeat
                        pv: ${IOC_MONO}:DAQ:HEARTBEAT
                py:
                self._wait_for_tick_or_timeout(self.sampletime)
        """
        if timeout_s is None or timeout_s < 0:
            timeout_s = 0.0

        with self._tick_cond:
            start_seq = self._tick_seq
            if self._stop_requested.is_set():
                return False

            # Wait until seq changes or timeout
            self._tick_cond.wait(timeout=timeout_s)
            if self._stop_requested.is_set():
                return False

            return self._tick_seq != start_seq

    def _trigger_worker_loop(self) -> None:
        while not self._trigger_worker_stop.is_set():
            ev = self._trigger_q.get()
            try:
                self._fire_triggers("monitor")
            except Exception:
                logger.exception("WORKER: Failed to fire monitor triggers (ev=%s)", ev)

    def _plugin_worker_loop(self) -> None:
        while not self._plugin_worker_stop.is_set():
            ev = self._plugin_q.get()

            for plugin in self.plugins:
                try:
                    plugin.on_monitor(ev)
                except Exception:
                    logger.exception( "Plugin '%s' failed handling monitor event %s",
                        getattr(plugin, "name", type(plugin).__name__), ev.pvname)
