# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional, Set, Union

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when raw scan configuration data is invalid."""

def filter_known_fields(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    """Filter out unknown fields from a dict for a given dataclass."""
    known_fields = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in known_fields}

@dataclass
class JogConfig:
    # PV to write jog velocity directly
    velocity_pv: Optional[str] = None
    abs_velocity: bool = False
    # PV to write a jog start command
    command_pv: Optional[str] = None
    # Command value for positive-direction jog (written to command_pv)
    command_pos: Optional[float] = None
    # Command value for negative-direction jog (written to command_pv)
    command_neg: Optional[float] = None

@dataclass
class ActuatorConfig:
    pv: Optional[str] = None
    type: str = "epics"
    rel_pv: Optional[str] = None
    rb_pv: Optional[str] = None
    cmd_pv: Optional[str] = None
    cmdvel_pv: Optional[str] = None
    stop_pv: Optional[str] = None
    stop_command: float = 0.0
    status_pv: Optional[str] = None
    queueing_delay: float = 0.01
    ca_timeout: float = 1.0
    auto_monitor: bool = True
    startup_timeout: float = 1.0
    in_position_band: float = -1.0
    dwell_time: float = 1.0
    ready_value: int = 0
    ready_bitmask: int = 0
    backlash: float = 0.0
    start_pv: Optional[str] = None
    start_command: float = 0.0
    velocity_pv: Optional[str] = None
    get_velocity_pv: Optional[str] = None
    velocity: float = 0.0
    jog: Optional[JogConfig] = None

    def resolve_pv(self, source: str) -> str:
        """Resolve an actuator source name to a configured PV name."""
        normalized_source = (source or "rbv").lower()

        if normalized_source == "rbv":
            pv = self.rb_pv or self.pv
        elif normalized_source in ("cmd", "set", "command"):
            pv = self.cmd_pv or self.pv
        elif normalized_source == "status":
            pv = self.status_pv
        elif normalized_source == "stop":
            pv = self.stop_pv
        elif normalized_source == "velocity":
            pv = (
                self.get_velocity_pv
                or self.velocity_pv
                or self.cmdvel_pv
                or self.pv
            )
        else:
            raise ValueError(
                f"Unsupported source {source!r}. "
                "Use rbv|cmd|status|stop|velocity."
            )

        if not pv:
            raise ValueError(
                "Actuator has no PV configured for source "
                f"{normalized_source!r}"
            )

        return pv

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActuatorConfig":
        # 1) Extract & convert the jog block if it exists
        jog_raw = data.get("jog")
        if isinstance(jog_raw, dict):
            # filter_known_fields should take a class and a dict,
            # returning only keys that match its __annotations__
            jog_kwargs = filter_known_fields(JogConfig, jog_raw)
            data["jog"] = JogConfig(**jog_kwargs)

        # 2) Now build the top‐level config
        cfg_kwargs = filter_known_fields(cls, data)
        return cls(**cfg_kwargs)


@dataclass(frozen=True)
class MonitorSpec:
    """One actuator monitor requested by a command-line argument."""

    name: str
    source: str = "rbv"
    pv: Optional[str] = None

    def resolve_pv(self, actuator_config: ActuatorConfig) -> str:
        """Return the direct PV override or resolve the actuator source."""
        if self.pv:
            return self.pv
        return actuator_config.resolve_pv(self.source)

    @classmethod
    def from_arg(cls, spec: str) -> "MonitorSpec":
        """Parse ``NAME``, ``NAME:SOURCE``, or ``NAME@PV``."""
        value = spec.strip()
        if not value:
            raise ValueError("Empty --monitor spec")

        if "@" in value:
            name, pv = value.split("@", 1)
            name = name.strip()
            pv = pv.strip()
            if not name or not pv:
                raise ValueError(
                    f"Invalid monitor spec {spec!r}, expected NAME@PV"
                )
            return cls(name=name, source="pv", pv=pv)

        if ":" in value:
            name, source = value.split(":", 1)
            name = name.strip()
            source = source.strip() or "rbv"
            if not name:
                raise ValueError(
                    f"Invalid monitor spec {spec!r}, empty NAME"
                )
            return cls(name=name, source=source)

        return cls(name=value)


@dataclass
class ScanDimension:
    """
    Represents one scan axis for an actuator in a multi-dimensional scan.
    """
    actuator: str
    start: float
    stop: float
    steps: int
    velocity: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScanDimension":
        """
        Construct a ScanDimension from a dict, filtering any unknown fields.
        """
        clean_data = filter_known_fields(cls, data)
        return cls(**clean_data)

    @classmethod
    def list_from_dicts(
        cls,
        data: Union[
            Dict[str, Any],
            List[Dict[str, Any]],
            Dict[str, Dict[str, Any]]
        ]
    ) -> List["ScanDimension"]:
        """
        Construct ScanDimension instances from:
        - a single dict with keys 'actuator', 'start', 'stop', 'steps'
        - a list of such dicts
        - a mapping of actuator name -> dict of start/stop/steps (e.g., from YAML under 'actuators')
        """
        dims: List[ScanDimension] = []  # type: ignore

        # Mapping of actuator -> params dict
        if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
            for actuator, params in data.items():
                merged = {"actuator": actuator, **params}
                clean = filter_known_fields(cls, merged)
                dims.append(cls(**clean))
            return dims

        # Single dict of parameters
        if isinstance(data, dict):
            return [cls.from_dict(data)]

        # List of dicts
        if isinstance(data, list):
            return [cls.from_dict(d) for d in data]

        raise TypeError(
            f"Unsupported data type for ScanDimension.list_from_dicts: {type(data)}"
        )

    @classmethod
    def from_dim_args(
        cls,
        dim_args: List[str]
    ) -> List["ScanDimension"]:
        """
        Parse a list of CLI "--dim" strings into a flat list of ScanDimension.
        Each string must be of form: actuator=NAME,start=VAL,stop=VAL,steps=N
        """
        def parse_single(dim_str: str) -> Dict[str, Any]:
            parts = dim_str.split(',')
            kv = dict(part.split('=') for part in parts)
            return {
                'actuator': kv['actuator'],
                'start': float(kv['start']),
                'stop': float(kv['stop']),
                'steps': int(kv['steps']),
                'velocity': float(kv.get('velocity', 0.0)),
            }

        return [cls(**parse_single(s)) for s in dim_args]

    def compute_positions_linear(self) -> List[float]:
        """
        Linearly generate a list of positions evenly spaced from start to stop.
        If steps < 2, returns [start].
        """
        if self.steps < 2:
            return [self.start]
        step = (self.stop - self.start) / (self.steps - 1)
        return [self.start + i * step for i in range(self.steps)]

    @staticmethod
    def get_actuators(
        dims: List["ScanDimension"]
    ) -> List[str]:
        """
        Extract actuator names from a list of ScanDimension.
        """
        return [d.actuator for d in dims]

@dataclass
class TriggerAction:
    pv: str
    value: Any
    delay: float = 0.0 

@dataclass
class ScanTriggers:
    before: List[TriggerAction] = field(default_factory=list)
    on_point: List[TriggerAction] = field(default_factory=list)
    after: List[TriggerAction] = field(default_factory=list)
    monitor: List[TriggerAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScanTriggers":
        if not isinstance(data, dict):
            logger.debug("Invalid triggers block: expected mapping, got %r", data)
            raise ConfigError("Invalid triggers: expected a mapping")

        def parse_action_list(key: str) -> List[TriggerAction]:
            raw = data.get(key, [])
            path = f"triggers.{key}"
            if not isinstance(raw, list):
                logger.debug("Invalid trigger phase at %s: expected list, got %r", path, raw)
                raise ConfigError(f"Invalid {path}: expected a list of mappings")
            if raw:
                logger.debug("Parsing trigger phase %r with %d action(s)", key, len(raw))

            actions = []
            for index, action in enumerate(raw):
                action_path = f"{path}[{index}]"
                if not isinstance(action, dict):
                    logger.debug("Invalid trigger action at %s: expected mapping, got %r", action_path, action)
                    raise ConfigError(f"Invalid {action_path}: expected a mapping")

                missing = [name for name in ("pv", "value") if name not in action]
                if missing:
                    logger.debug("Invalid trigger action at %s: missing=%s, action=%r", action_path, missing, action)
                    raise ConfigError(
                        f"Invalid {action_path}: missing required key(s): "
                        f"{', '.join(missing)}. Each trigger action must contain "
                        "'pv' and 'value' in the same mapping."
                    )

                try:
                    actions.append(TriggerAction(**action))
                except TypeError as exc:
                    logger.debug("Invalid trigger action at %s: %s", action_path, exc)
                    raise ConfigError(f"Invalid {action_path}: {exc}") from exc
            return actions

        triggers = cls(
            before=parse_action_list("before"),
            on_point=parse_action_list("on_point"),
            after=parse_action_list("after"),
            monitor=parse_action_list("monitor"),
        )
        for key in data:
            if key not in {"before", "on_point", "after", "monitor"}:
                setattr(triggers, key, parse_action_list(key))

        return triggers

@dataclass
class SubscriptionConfig:
    name: str
    role: str
    # exactly one of these must be set
    pv: Optional[str] = None
    actuator: Optional[str] = None
    # used only when actuator is set
    source: Optional[str] = None
    timeout: Optional[float] = None

    @staticmethod
    def _is_configured(value: Optional[str]) -> bool:
        return value is not None and bool(str(value).strip())

    def validate(self) -> None:
        """ Validate subscription config before scan. """
        name = "" if self.name is None else str(self.name).strip()
        role = "" if self.role is None else str(self.role).strip()

        if not name:
            raise ValueError("Subscription must define a non-empty name")
        if not role:
            raise ValueError( f"Subscription {name} must define a non-empty role")

        has_pv = self._is_configured(self.pv)
        has_actuator = self._is_configured(self.actuator)

        if has_pv and has_actuator:
            raise ValueError(f"Subscription '{name}' must define exactly one of 'pv' or 'actuator', not both")
        if not has_pv and not has_actuator:
            raise ValueError(
                f"Subscription '{name}' must define exactly one of "
                "'pv' or 'actuator'. For a direct PV subscription, "
                "add a 'pv' field."
            )
        if self._is_configured(self.source) and not has_actuator:
            raise ValueError(
                f"Subscription '{name}' defines 'source', but 'source' is "
                "only valid together with 'actuator'"
            )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubscriptionConfig":
        if not isinstance(data, dict):
            raise TypeError(f"Subscription entry must be a mapping, got {type(data).__name__}")

        missing = [key for key in ("name", "role") if key not in data]
        if missing:
            raise ValueError("Subscription entry missing, required: "
                + ", ".join(missing)
            )
        # timeout is optional. When present, accept numbers and numeric strings.
        raw_timeout = data.get("timeout")

        if raw_timeout is None:
            timeout = None
        else:
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError( f"timeout must be a number, got {raw_timeout!r}") from exc
            if timeout < 0:
                raise ValueError("timeout must be >= 0")

        config = cls(
            name=data["name"],
            role=data["role"],
            pv=data.get("pv"),
            actuator=data.get("actuator"),
            source=data.get("source"),
            timeout=timeout,
        )
        config.validate()
        return config


def validate_subscription_names(subscriptions: Iterable[SubscriptionConfig]) -> None:
    """Reject duplicate names across all roles; distinct names may share a PV."""
    seen_names: Set[str] = set()
    for subscription in subscriptions:
        name = subscription.name
        if name in seen_names:
            raise ValueError(f"Duplicate subscription name: {name!r}")
        seen_names.add(name)


@dataclass
class PluginConfig:
    """Common plugin declaration with plugin-specific parameters left untyped."""
    type: str
    name: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate only the common plugin envelope."""
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("Plugin entry must define a non-empty 'type'")
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("Plugin 'name' must be a string when provided")
        if not isinstance(self.parameters, dict):
            raise TypeError("Plugin 'parameters' must be a mapping")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PluginConfig":
        if not isinstance(data, dict):
            raise TypeError(
                f"Plugin entry must be a mapping, got {type(data).__name__}"
            )
        if "type" not in data:
            raise ValueError("Plugin entry missing required: type")

        raw_parameters = data.get("parameters", {})
        if raw_parameters is None:
            raw_parameters = {}
        if not isinstance(raw_parameters, dict):
            raise TypeError("Plugin 'parameters' must be a mapping")

        config = cls(
            type=data["type"],
            name=data.get("name"),
            parameters=dict(raw_parameters),
        )
        config.validate()
        return config

@dataclass
class MonitorConfig:
    """Untyped monitor parameter block.

    The selected monitor implementation is chosen by the top-level
    ``monitor_type`` field. This block contains only monitor-specific
    parameters, similar to the ``parameters`` block used by plugins.
    """
    parameters: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MonitorConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            logger.error("monitor config must be a mapping, got %s", type(data))
            return cls()
        # The monitor type remains the top-level ScanConfig.monitor_type.
        # The preferred YAML layout is:
        #   monitor_type: print
        #   monitor:
        #     format: tsv
        #     include_timestamps: true
        #
        # Be tolerant of the earlier transitional layout:
        #   monitor:
        #     parameters:
        #       format: tsv
        # so old configs do not silently fall back to defaults.
        if set(data.keys()) == {"parameters"} and isinstance(data.get("parameters"), dict):
            return cls(parameters=dict(data["parameters"]))

        return cls(parameters=dict(data))


@dataclass
class ScanConfig:
    actuators: Dict[str, ActuatorConfig]
    detector_pvs: List[str]
    detector_pvs_monitor: bool = True
    scan_dimensions: Optional[List[ScanDimension]] = None
    parallel_scans: Optional[List[ScanDimension]] = None
    nested_scans: Optional[List[ScanDimension]] = None
    plugin_configs: List[PluginConfig] = field(default_factory=list)
    monitor_type: Optional[str] = None
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    stop_pv: Optional[str] = None
    data_dir: str = "."
    output_file: str = "scan_results.txt"
    include_timestamps: bool = False
    timestamp_output_format: str = "iso8601"
    integration_time: float = 0.0
    sample_rate_hz: float = 1.0
    debug: bool = False
    performance_report: bool = False
    data_writing_enabled: bool = True
    manifest_mode: str = "full"
    triggers: Optional[ScanTriggers] = None
    metadata_pvs: List[str] = field(default_factory=list)          # EPICS PVs to monitor in parallel
    metadata_constants: Dict[str, Any] = field(default_factory=dict)  # key/value string constants
    metadata_file: str = "scan_metadata.txt"                        # sidecar filename
    subscriptions: List[SubscriptionConfig] = field(default_factory=list)
    detector_reader_strategy: str = "direct"

    def validate(self) -> None:
        """ Validate and normalize configuration via API """
        actuators_raw = self.actuators or {}
        if not isinstance(actuators_raw, dict):
            raise TypeError("'actuators' must be a mapping")

        validated_actuators: Dict[str, ActuatorConfig] = {}
        for name, actuator in actuators_raw.items():
            if isinstance(actuator, dict):
                actuator = ActuatorConfig.from_dict(dict(actuator))
            elif not isinstance(actuator, ActuatorConfig):
                raise TypeError(
                    f"Actuator config for {name!r} must be a mapping or "
                    f"ActuatorConfig, got {type(actuator).__name__}"
                )
            validated_actuators[name] = actuator

        self.actuators = validated_actuators

        self.detector_reader_strategy = self._normalize_detector_reader_strategy(
            self.detector_reader_strategy
        )
        self.timestamp_output_format = self._normalize_timestamp_output_format(
            self.timestamp_output_format
        )
        subscriptions_raw = self.subscriptions or []
        if not isinstance(subscriptions_raw, list):
            raise TypeError("'subscriptions' must be a list of mappings")

        validated_subscriptions: List[SubscriptionConfig] = []
        for index, subscription in enumerate(subscriptions_raw):
            try:
                if isinstance(subscription, dict):
                    subscription = SubscriptionConfig.from_dict(subscription)
                elif isinstance(subscription, SubscriptionConfig):
                    subscription.validate()
                else:
                    raise TypeError(
                        "Subscription entry must be a mapping or "
                        f"SubscriptionConfig, got {type(subscription).__name__}"
                    )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid subscriptions[{index}]: {exc}") from exc
            validated_subscriptions.append(subscription)

        validate_subscription_names(validated_subscriptions)
        self.subscriptions = validated_subscriptions

    @staticmethod
    def _normalize_timestamp_output_format(value: Any) -> str:
        """Return the canonical scan-file timestamp representation."""
        normalized = str(value).strip().lower().replace("-", "").replace("_", "")
        aliases = {
            "iso": "iso8601",
            "iso8601": "iso8601",
            "unix": "unix",
            "posix": "unix",
            "raw": "unix",
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(
                "timestamp_output_format must be one of: iso8601, unix "
                f"(got {value!r})"
            ) from exc

    @staticmethod
    def _normalize_detector_reader_strategy(strategy: Any) -> str:
        """Return the canonical detector-reader strategy name."""
        normalized = str(strategy).strip().lower().replace("-", "_")
        if normalized == "monitor_snapshot":
            normalized = "snapshot"
        if normalized not in {"direct", "snapshot"}:
            raise ValueError(
                "detector_reader_strategy must be one of: direct, snapshot "
                f"(got {strategy!r})"
            )
        return normalized

    @staticmethod
    def _parse_dimensions(
        config_dict: Dict[str, Any],
        key: str,
    ) -> Optional[List[ScanDimension]]:
        dimensions = config_dict.get(key)
        if dimensions is None:
            return None
        return [
            ScanDimension.from_dict(dimension)
            for dimension in dimensions
            if isinstance(dimension, dict)
        ]


    @staticmethod
    def _parse_plugin_configs(
        config_dict: Dict[str, Any],
    ) -> List[PluginConfig]:
        plugins_raw = config_dict.get("plugin_configs", [])
        if plugins_raw is None:
            plugins_raw = []
        if not isinstance(plugins_raw, list):
            raise TypeError("'plugin_configs' must be a list of mappings")

        plugins: List[PluginConfig] = []
        for index, plugin_data in enumerate(plugins_raw):
            try:
                if isinstance(plugin_data, PluginConfig):
                    plugin_data.validate()
                    plugin = plugin_data
                else:
                    plugin = PluginConfig.from_dict(plugin_data)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid plugin_configs[{index}]: {exc}") from exc
            plugins.append(plugin)
        return plugins

    @staticmethod
    def _parse_subscriptions(
        config_dict: Dict[str, Any],
    ) -> List[SubscriptionConfig]:
        subscriptions_raw = config_dict.get("subscriptions", [])
        if subscriptions_raw is None:
            subscriptions_raw = []
        if not isinstance(subscriptions_raw, list):
            raise TypeError("'subscriptions' must be a list of mappings")

        subscriptions = []
        for index, subscription_data in enumerate(subscriptions_raw):
            try:
                subscriptions.append(SubscriptionConfig.from_dict(subscription_data))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid subscriptions[{index}]: {exc}") from exc
        validate_subscription_names(subscriptions)
        return subscriptions

    @staticmethod
    def _parse_manifest_mode(config_dict: Dict[str, Any]) -> str:
        manifest_mode = str(config_dict.get("manifest_mode", "full")).strip().lower()
        if manifest_mode not in {"full", "small", "off"}:
            raise ValueError(
                "manifest_mode must be one of: full, small, off "
                f"(got {config_dict.get('manifest_mode')!r})"
            )
        logger.debug("manifest_mode: %s", manifest_mode)
        return manifest_mode

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "ScanConfig":
        known_keys = {f.name for f in fields(cls)}
        logger.debug("Creating ScanConfig from dict, known_keys = %r", known_keys)
        actuators = {
            name: ActuatorConfig.from_dict(cfg)
            for name, cfg in config_dict.get("actuators", {}).items()
            if isinstance(cfg, dict)
        }

        triggers_raw = config_dict.get("triggers", None)
        triggers = ScanTriggers.from_dict(triggers_raw) if triggers_raw else None

        plugin_configs = cls._parse_plugin_configs(config_dict)
        subscriptions = cls._parse_subscriptions(config_dict)
        monitor = MonitorConfig.from_dict(config_dict.get("monitor"))
        manifest_mode = cls._parse_manifest_mode(config_dict)

        # Log unknown configuration keys
        for key, value in config_dict.items():
            if key not in known_keys:
                logger.debug("Unknown key in ScanConfig YAML: %s → %r", key, value)
        return cls(
            actuators=actuators,
            detector_pvs=config_dict.get("detector_pvs", []),
            detector_pvs_monitor=config_dict.get("detector_pvs_monitor", True),
            detector_reader_strategy=config_dict.get(
                "detector_reader_strategy",
                "direct",
            ),
            scan_dimensions=cls._parse_dimensions(config_dict, "scan_dimensions"),
            parallel_scans=cls._parse_dimensions(config_dict, "parallel_scans"),
            nested_scans=cls._parse_dimensions(config_dict, "nested_scans"),
            plugin_configs=plugin_configs,
            monitor_type=config_dict.get("monitor_type", None),
            monitor=monitor,
            stop_pv=config_dict.get("stop_pv") or None,
            data_dir=config_dict.get("data_dir", "."),
            output_file=config_dict.get("output_file", "scan_results.txt"),
            include_timestamps=config_dict.get("include_timestamps", False),
            timestamp_output_format=config_dict.get(
                "timestamp_output_format",
                "iso8601",
            ),
            integration_time=config_dict.get("integration_time", 0.0),
            sample_rate_hz=config_dict.get("sample_rate_hz", 1.0),
            debug=config_dict.get("debug", False),
            performance_report=config_dict.get("performance_report", False),
            data_writing_enabled=config_dict.get("data_writing_enabled", True),
            manifest_mode=manifest_mode,
            triggers=triggers,
            metadata_pvs=config_dict.get("metadata_pvs", []),
            metadata_constants=config_dict.get("metadata_constants", {}),
            metadata_file=config_dict.get("metadata_file", "scan_metadata.txt"),
            subscriptions=subscriptions,
        )


def build_scan_dim(actuator: str, start: float, stop: float, steps: int) -> ScanDimension:
    """
    Construct a ScanDimension. TODO: replace
    Args:
        actuator (str): Name of the actuator (e.g. "energy", "pitch").
        start (float): Start position of the scan.
        stop (float): Stop position of the scan.
        steps (int): Number of scan points.
    Returns:
        ScanDimension: Configured scan dimension object.
    """
    return ScanDimension(actuator=actuator, start=start, stop=stop, steps=steps)
