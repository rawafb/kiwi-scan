from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any, Callable, ClassVar, Dict, List, Optional, Union


class FakeRawPV:
    """Minimal raw-PV stub used behind fake EpicsPV-like wrappers."""

    def __init__(self) -> None:
        self.removed: List[int] = []
        self.cleared = False
        self.disconnected = False

    def remove_callback(self, idx: int) -> None:
        self.removed.append(idx)

    def clear_callbacks(self) -> None:
        self.cleared = True

    def disconnect(self) -> None:
        self.disconnected = True


class FakePV:
    """
    Emulate the behavior and create role-specific subclasses needed by the current tests:
      * SubscriptionManager: EpicsPV
      * EpicsActuator: monitor PV wrappers
      * TriggerManager: trigger PV
    """

    instances: ClassVar[List[FakePV]] = []
    next_index: int = 1
    callback_mode: str = "list"  # "list" or "dict"
    track_instances: bool = False
    use_raw_pv: bool = False
    track_put_calls: bool = False
    default_get_value: Any = None

    def __init__(
        self,
        pvname: str,
        timeout: float = 1.0,
        queueing_delay: float = 0.01,
        auto_monitor: bool = False,
        **kwargs: Any,
    ) -> None:
        self.pvname = pvname
        self.timeout = timeout
        self.queueing_delay = queueing_delay
        self.auto_monitor = auto_monitor
        self.kwargs = dict(kwargs)
        self.clear_callbacks_called = False
        self.disconnected = False
        self.connected = True
        self.timestamp = 0.0
        self.severity = 0
        self.status = 0
        self._value = self.__class__.default_get_value
        self.get_calls: List[Dict[str, Any]] = []
        self.get_responses: List[Any] = []
        self.put_calls: List[Any] = []
        self.put_result = True
        self.put_exception: Optional[Exception] = None
        self.check_pv_calls = 0
        self.check_pv_exception: Optional[Exception] = None
        self.wait_for_connection_calls: List[Optional[float]] = []
        self.wait_for_connection_result = True
        self.add_callback_kwargs: List[Dict[str, Any]] = []
        self.created_via_create_monitor = False

        self.callbacks: Union[
            Dict[int, Callable[..., None]],
            List[Callable[..., None]],
        ]
        if self.__class__.callback_mode == "dict":
            self.callbacks = {}
        else:
            self.callbacks = []

        if self.__class__.use_raw_pv:
            self._pv = FakeRawPV()
        else:
            self._pv = SimpleNamespace(disconnect=self._disconnect)

        if self.__class__.track_instances:
            self.__class__.instances.append(self)

    def wait_for_connection(self, timeout: Optional[float] = None) -> bool:
        self.wait_for_connection_calls.append(timeout)
        return self.wait_for_connection_result

    def add_callback(self, callback: Callable[..., None], **kwargs: Any) -> int:
        self.add_callback_kwargs.append(dict(kwargs))
        if isinstance(self.callbacks, dict):
            idx = self.__class__.next_index
            self.__class__.next_index += 1
            self.callbacks[idx] = callback
            return idx

        self.callbacks.append(callback)
        return len(self.callbacks)

    def remove_callback(self, idx: int) -> None:
        if self.__class__.use_raw_pv:
            self._pv.remove_callback(idx)

        if isinstance(self.callbacks, dict):
            self.callbacks.pop(idx, None)
            return
        if 1 <= idx <= len(self.callbacks):
            self.callbacks.pop(idx - 1)

    def clear_callbacks(self) -> None:
        self.clear_callbacks_called = True
        if isinstance(self.callbacks, dict):
            self.callbacks.clear()
        else:
            self.callbacks.clear()

    def trigger(self, idx: int, *, value: Any = None, **kwargs: Any) -> None:
        """Invoke one stored callback in monitor-style tests."""
        if isinstance(self.callbacks, dict):
            cb = self.callbacks[idx]
        else:
            cb = self.callbacks[idx - 1]
        cb(pvname=self.pvname, value=value, **kwargs)

    def get(self, timeout: Optional[float] = None, use_monitor: bool = False) -> Any:
        self.get_calls.append({"timeout": timeout, "use_monitor": use_monitor})
        if self.get_responses:
            return self.get_responses.pop(0)
        return self._value

    def put(self, value: Any, timeout: Optional[float] = None) -> bool:
        if self.__class__.track_put_calls:
            self.put_calls.append(value)
        self.last_written = value
        if self.put_exception is not None:
            raise self.put_exception
        if self.put_result:
            self._value = value
        return self.put_result

    def check_pv(self) -> None:
        self.check_pv_calls += 1
        if self.check_pv_exception is not None:
            raise self.check_pv_exception

    def disconnect(self) -> None:
        if self.__class__.use_raw_pv:
            self._pv.disconnect()
        self._disconnect()

    def _disconnect(self) -> None:
        self.disconnected = True


def make_fake_epics_module(*, pv_class: Optional[type] = None) -> ModuleType:
    """Create a minimal ``epics`` module stub backed by ``FakePV``."""
    mod = ModuleType("epics")
    mod.PV = (  # pyright: ignore[reportAttributeAccessIssue]
        pv_class or make_fake_epics_pv_class()
    )
    mod.ca = SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        poll=lambda: None,
        use_initial_context=lambda: None,
    )
    return mod


def make_fake_epics_pv_class():
    """EpicsPV fake for SubscriptionManager tests."""

    class FakeEpicsPV(FakePV):
        instances: ClassVar[List[FakePV]] = []
        track_instances = True
        callback_mode = "list"
        use_raw_pv = False
        track_put_calls = False

    return FakeEpicsPV


def make_fake_monitor_pv_class(*, start_index: int = 100):
    """Monitor-capable fake for actuator tests."""

    class FakeMonitorPV(FakePV):
        instances: ClassVar[List[FakePV]] = []
        next_index = start_index
        track_instances = False
        callback_mode = "dict"
        use_raw_pv = True
        track_put_calls = False
        default_get_value = 0

    return FakeMonitorPV


def make_fake_actuator_pv_class(*, start_index: int = 100):
    """Configurable PV fake for unit-testing EPICS actuator behavior."""

    class FakeActuatorPV(FakePV):
        instances: ClassVar[List[FakePV]] = []
        next_index = start_index
        track_instances = True
        callback_mode = "dict"
        use_raw_pv = True
        track_put_calls = True

        @classmethod
        def create_monitor(cls, pvname: str, **kwargs: Any) -> FakePV:
            monitor = cls(pvname, **kwargs)
            monitor.created_via_create_monitor = True
            return monitor

    return FakeActuatorPV


def make_fake_trigger_pv_class():
    """Trigger fake that only records put() calls."""

    class FakeTriggerPV(FakePV):
        instances: ClassVar[List[FakePV]] = []
        track_instances = False
        callback_mode = "list"
        use_raw_pv = False
        track_put_calls = True

    return FakeTriggerPV


def make_fake_metadata_pv_class():
    """Metadata-monitor fake sharing callback and lifecycle behavior with FakePV."""

    class FakeMetadataPV(FakePV):
        default_get_value = 1

        def __init__(self, pvname: str, **kwargs: Any) -> None:
            super().__init__(pvname, **kwargs)
            self.timestamp = 1000.0

        def get_with_metadata(self, **kwargs: Any) -> Dict[str, Any]:
            return {
                "pvname": self.pvname,
                "value": self.get(**kwargs),
                "timestamp": self.timestamp,
                "severity": self.severity,
                "status": self.status,
            }

        def check_pv(self) -> None:
            super().check_pv()
            if not self.connected:
                raise ConnectionError(self.pvname)

        def disconnect(self) -> None:
            super().disconnect()
            self.connected = False

    return FakeMetadataPV


class FakeMonitorProvider:
    def __init__(self) -> None:
        self.callbacks_by_pv: Dict[
            str,
            Optional[Callable[..., None]],
        ] = {}
        self.removed_pvs: List[str] = []

    def supports_monitors(self) -> bool:
        return True

    def add_monitor(
        self,
        pvname: str,
        user_callback: Optional[Callable[..., None]] = None,
        **kwargs: Any,
    ) -> Dict[str, str]:
        self.callbacks_by_pv[pvname] = user_callback
        return {"pvname": pvname}

    def remove_monitor(self, pvname: str) -> None:
        self.removed_pvs.append(pvname)


class FakeNoMonitorBackend:
    def supports_monitors(self) -> bool:
        return False
