"""GAssistPlugin — lifecycle, execute dispatch, and the confirmation flow.

Design components table + Requirement 1/9 + task 15: maps engine requests to domain
services and formats NL-friendly responses through the error mapper. Domain values never
reach the adapter unvalidated (validation lives in the domain services / validator); this
layer only parses function arguments, decides confirmation, and formats results.

Wire conventions (stdlib fallback, libs/README.txt): engine requests arrive via the
``GAssistProtocol`` loop; this plugin answers JSON-RPC responses for
initialize/ping/shutdown and emits ``complete`` notifications for every ``execute``. A
``complete`` notification params shape is ``{"success": bool, "message": str,
"data": {...}}``. Risky ``execute`` outcomes are a success ``complete`` whose data carries
``needs_confirmation: true`` + ``confirm_token`` (never an error — design confirmation
flow); genuine failures are ``complete`` with ``success: false`` (typed error code + user
message). Unknown functions get a JSON-RPC method-not-found response and running state is
retained (Req 1.7).
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import clamp_result_message, user_message_for
from ..integration.client import AfterburnerClient
from ..integration.profiles import ProfileFileReader
from ..models import (
    ControlFeature,
    ControlResult,
    ErrorCode,
    FanCurve,
    FanCurvePoint,
    PluginError,
)
from ..safety.capabilities import HardwareCapabilityResolver
from ..safety.policy import SafetyPolicy
from ..safety.validator import TuningValidator
from ..services.diagnostics import DiagnosticsService
from ..services.optimize import OptimizeService
from ..services.ownership import TuningOwnershipService
from ..services.profiles import ProfileManager
from ..services.telemetry import TelemetryService
from . import transport

PASSTHROUGH_TIMEOUT_SECONDS = 60.0  # Req 1.11: unconfirmed input auto-cancels

# Protocol method timings (Requirements 1.3/1.5/1.6/1.9)
INITIALIZE_BUDGET_S = 5.0
PING_BUDGET_S = 1.0
EXECUTE_BUDGET_S = 30.0
SHUTDOWN_BUDGET_S = 5.0


@dataclass
class PluginServices:
    """The domain stack the plugin dispatches to (tests assemble with FakeAfterburner)."""

    client: AfterburnerClient
    telemetry: TelemetryService
    resolver: HardwareCapabilityResolver
    validator: TuningValidator
    profiles: ProfileManager
    ownership: TuningOwnershipService
    optimize: OptimizeService
    diagnostics: DiagnosticsService
    policy: SafetyPolicy


def build_services(
    interface,
    profiles_dir: Optional[Path] = None,
    *,
    clock: Optional[Callable[[], float]] = None,
    tolerance: float = 1.0,
    optimize_read_interval_s: float = 0.0,
    optimize_verify_timeout_s: float = 30.0,
) -> PluginServices:
    """Assemble the domain stack around one AfterburnerInterface (mock or real).

    ``optimize_read_interval_s`` is 0 for tests (deterministic single read-back); the
    plugin entry point keeps the real 1 s cadence by passing the default here.
    """
    client = AfterburnerClient(interface=interface)
    reader = ProfileFileReader(profiles_dir or Path(""))  # absent dir => no profiles
    telemetry = TelemetryService(interface, clock=clock)
    resolver = HardwareCapabilityResolver(interface)
    validator = TuningValidator(resolver)
    profiles = ProfileManager(client, reader, tolerance=tolerance)
    ownership = TuningOwnershipService(client, profiles)
    optimize = OptimizeService(
        client,
        verify_timeout_s=optimize_verify_timeout_s,
        read_interval_s=optimize_read_interval_s,
    )
    diagnostics = DiagnosticsService(telemetry)
    policy = SafetyPolicy(clock=clock)
    return PluginServices(
        client=client,
        telemetry=telemetry,
        resolver=resolver,
        validator=validator,
        profiles=profiles,
        ownership=ownership,
        optimize=optimize,
        diagnostics=diagnostics,
        policy=policy,
    )


# --------------------------------------------------------------------------- jsonable
def jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums/tuples into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return str(value)


# --------------------------------------------------------------------------- executors
@dataclass
class ExecResult:
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


Executor = Callable[[Dict[str, Any], int], ExecResult]


def _gpu_of(args: Dict[str, Any], default: int = 0) -> int:
    value = args.get("gpu_index", default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            "That GPU index isn't valid.",
            detail=f"gpu_index {value!r}",
        )
    return value


def _require_number(args: Dict[str, Any], key: str, label: str) -> float:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"Please provide a numeric {label}.",
                detail=f"{key}={value!r}",
            )
    number = float(value)
    if not math.isfinite(number):
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            f"Please provide a numeric {label}.",
            detail=f"{key}={value!r}",
        )
    return number


# Feature mapping used by the set_* executors and the decision-time no-op check.
_SINGLE_VALUE_FEATURE = {
    "set_power_limit": (ControlFeature.POWER_LIMIT, "percent"),
    "set_core_offset": (ControlFeature.CORE_OFFSET, "offset_mhz"),
    "set_memory_offset": (ControlFeature.MEMORY_OFFSET, "offset_mhz"),
    "set_fan_percent": (ControlFeature.FAN_PERCENT, "percent"),
}


class _ExecutionError(Exception):
    """Marks a PluginError raised from an executor (already typed)."""


def apply_single_value(
    services: PluginServices,
    gpu_index: int,
    feature: ControlFeature,
    requested: float,
) -> ExecResult:
    """Validator-clamped apply of one single-value control (never an unvalidated write)."""
    safe_value, clamped = services.validator.validate_and_clamp(
        gpu_index, feature, requested
    )
    services.client.apply_control(gpu_index, feature, safe_value)
    result = ControlResult(
        feature=feature,
        requested_value=requested,
        applied_value=safe_value,
        clamped=clamped,
        applied=True,
        message="",
    )
    message = clamp_result_message(result)
    return ExecResult(
        message=message,
        data={
            "feature": feature.value,
            "value": safe_value,
            "requested": requested,
            "clamped": clamped,
            "applied": True,
        },
    )


def apply_fan_curve(
    services: PluginServices, gpu_index: int, raw_points: Any
) -> ExecResult:
    """Validate/clamp + apply a fan curve from [[tempC, fanPct], ...] points."""
    if not isinstance(raw_points, (list, tuple)) or not raw_points:
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            "A fan curve needs a list of [temperature, fan speed] points.",
        )
    try:
        points = tuple(
            FanCurvePoint(temp_c=float(p[0]), fan_percent=float(p[1]))
            for p in raw_points
        )
    except (TypeError, ValueError, IndexError):
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            "Fan curve points must be [temperature, fan speed] pairs.",
        )
    curve = services.validator.validate_fan_curve(gpu_index, FanCurve(points=points))
    result = services.client.apply_fan_curve(gpu_index, curve)
    return ExecResult(
        message=f"Fan curve with {len(curve.points)} points applied.",
        data={
            "feature": ControlFeature.FAN_CURVE.value,
            "points": len(curve.points),
            "applied": True,
            "message": result.message,
        },
    )


def _telemetry_message(t) -> str:
    parts = [
        f"{t.gpu_name}",
        f"{t.temperature_c:.1f}°C" if t.temperature_c is not None else "temp n/a",
        f"{t.utilization_pct:.0f}% util" if t.utilization_pct is not None else "util n/a",
        f"{t.core_clock_mhz:.0f} MHz" if t.core_clock_mhz is not None else "clock n/a",
        f"fan {t.fan_percent:.0f}%" if t.fan_percent is not None else None,
        f"power {t.power_watts:.0f}W" if t.power_watts is not None else None,
        f"VRAM {t.memory_used_mb:.0f}/{t.memory_total_mb:.0f} MB"
        if t.memory_used_mb is not None and t.memory_total_mb is not None
        else None,
    ]
    text = ", ".join(part for part in parts if part)
    if t.is_stale:
        text += " (readings are STALE — Afterburner may be restarting)"
    return text


# --------------------------------------------------------------------------- plugin
class GAssistPlugin:
    """Lifecycle + dispatch + confirmation flow over a domain service stack."""

    def __init__(
        self,
        services: PluginServices,
        *,
        tolerance: float = 1.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.services = services
        self.tolerance = tolerance
        self._clock = clock or _time.monotonic
        self.initialized = False
        self.shutdown_requested = False
        self.keep_session = False
        self._startup_status = "not_started"
        self._startup_detail = ""
        # Pending passthrough-input confirmations: function -> {args, expires_at, gpu}
        self._pending_input: Dict[str, Dict[str, Any]] = {}
        self._registry: Dict[str, Executor] = self._build_registry()
        self._startup_ms: Optional[float] = None

    # ------------------------------------------------------------------ registry
    def _build_registry(self) -> Dict[str, Executor]:
        s = self.services

        def get_gpu_status(args: Dict[str, Any], gpu: int) -> ExecResult:
            t = s.telemetry.get_status(gpu)
            return ExecResult(
                message=_telemetry_message(t),
                data={"gpu_index": gpu, "telemetry": t},
            )

        def get_gpu_limits(args: Dict[str, Any], gpu: int) -> ExecResult:
            caps = s.resolver.resolve(gpu)
            lines: List[str] = []
            for feature, rng in (
                (ControlFeature.POWER_LIMIT, caps.limits.power_limit_pct),
                (ControlFeature.CORE_OFFSET, caps.limits.core_offset_mhz),
                (ControlFeature.MEMORY_OFFSET, caps.limits.memory_offset_mhz),
                (ControlFeature.FAN_PERCENT, caps.limits.fan_percent),
            ):
                if rng is not None and caps.supports(feature):
                    lines.append(
                        f"{feature.value}: {rng.min:g} to {rng.max:g}"
                    )
            if caps.supports(ControlFeature.FAN_CURVE):
                lines.append(f"fan curve: up to {caps.limits.fan_curve_max_points} points")
            status = caps.control_interface_status.value
            if not lines:
                lines.append("no write controls available (read-only)")
            message = "Supported tuning ranges: " + "; ".join(lines)
            if status != "ok":
                message += f" (control interface: {status})"
            return ExecResult(
                message=message,
                data={
                    "gpu_index": gpu,
                    "supported": sorted(f.value for f in caps.supported_controls),
                    "control_interface_status": status,
                },
            )

        def get_tuning_state(args: Dict[str, Any], gpu: int) -> ExecResult:
            state = s.client.read_tuning_state(gpu)
            return ExecResult(message=_state_message(state), data=state)

        def get_profiles(args: Dict[str, Any], gpu: int) -> ExecResult:
            profiles = s.profiles.get_profiles(gpu)
            if not profiles:
                return ExecResult(
                    message="No Afterburner profiles are saved.",
                    data={"profiles": [], "active_profile_id": None},
                )
            labels = []
            active_id = None
            for profile in profiles:
                active_id = profile.id if profile.is_active else active_id
                labels.append(
                    f"{profile.name}" + (" (active)" if profile.is_active else "")
                )
            message = "Profiles: " + ", ".join(labels)
            return ExecResult(
                message=message,
                data={"profiles": profiles, "active_profile_id": active_id},
            )

        def get_tuning_ownership(args: Dict[str, Any], gpu: int) -> ExecResult:
            report = s.ownership.build_report(gpu)
            return ExecResult(message=report.summary, data=report)

        def show_configuration(args: Dict[str, Any], gpu: int) -> ExecResult:
            status = s.client.detect()
            version = s.client.get_version()
            caps = s.resolver.resolve(gpu)
            message = (
                f"MSI Afterburner: {status.value}"
                + (f" (version {version})" if version else "")
            )
            try:
                profile_count = len(s.profiles.get_profiles(gpu))
            except PluginError:
                profile_count = None
            if profile_count is not None:
                message += f"; {profile_count} profile(s)"
            supported = sorted(f.value for f in caps.supported_controls)
            message += "; available controls: " + (", ".join(supported) or "none")
            return ExecResult(
                message=message,
                data={
                    "interface_status": status.value,
                    "version": version,
                    "supported_controls": supported,
                    "profile_count": profile_count,
                },
            )

        def load_profile(args: Dict[str, Any], gpu: int) -> ExecResult:
            raw = args.get("profile_id", args.get("name"))
            profile_id = _parse_profile_ref(raw)
            result = s.profiles.load_profile(profile_id, gpu)
            return ExecResult(
                message=result.message,
                data={"profile_id": profile_id, "applied": True},
            )

        def reset_tuning(args: Dict[str, Any], gpu: int) -> ExecResult:
            result = s.profiles.reset_profile(gpu)
            return ExecResult(message=result.message, data={"applied": True})

        def diagnose(args: Dict[str, Any], gpu: int) -> ExecResult:
            diagnosis = s.diagnostics.diagnose_performance(gpu)
            evidence = list(diagnosis.evidence)
            message = (
                f"{diagnosis.summary} (classified: {diagnosis.cause.value}, "
                f"confidence {diagnosis.confidence:.0%})"
            )
            return ExecResult(
                message=message,
                data={
                    "cause": diagnosis.cause.value,
                    "confidence": diagnosis.confidence,
                    "evidence": evidence,
                    "summary": diagnosis.summary,
                },
            )

        def set_value(args: Dict[str, Any], gpu: int) -> ExecResult:
            function_name = args.get("__function", "")
            feature, key = _SINGLE_VALUE_FEATURE[function_name]
            requested = _require_number(args, key, feature.value)
            return apply_single_value(self.services, gpu, feature, requested)

        def set_fan_curve(args: Dict[str, Any], gpu: int) -> ExecResult:
            return apply_fan_curve(self.services, gpu, args.get("points"))

        def optimize_quiet(args: Dict[str, Any], gpu: int) -> ExecResult:
            result = s.optimize.optimize_quiet(gpu)
            return ExecResult(message=result.message, data=_result_data(result))

        def optimize_thermal(args: Dict[str, Any], gpu: int) -> ExecResult:
            target = args.get("target_c")
            result = s.optimize.optimize_thermal(target, gpu)
            return ExecResult(message=result.message, data=_result_data(result))

        registry: Dict[str, Executor] = {
            "get_gpu_status": get_gpu_status,
            "get_gpu_limits": get_gpu_limits,
            "get_tuning_state": get_tuning_state,
            "get_profiles": get_profiles,
            "get_tuning_ownership": get_tuning_ownership,
            "show_configuration": show_configuration,
            "load_profile": load_profile,
            "reset_tuning": reset_tuning,
            "diagnose_performance": diagnose,
            "set_power_limit": set_value,
            "set_core_offset": set_value,
            "set_memory_offset": set_value,
            "set_fan_percent": set_value,
            "set_fan_curve": set_fan_curve,
            "optimize_quiet": optimize_quiet,
            "optimize_thermal": optimize_thermal,
        }
        return registry

    def registered_functions(self) -> Sequence[str]:
        return tuple(sorted(self._registry))

    # ------------------------------------------------------------------ lifecycle
    def initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = self._clock()
        self._run_startup_sequence()
        elapsed_ms = (self._clock() - started) * 1000.0
        self._startup_ms = elapsed_ms
        self.initialized = True
        return {
            "status": self._startup_status,
            "message": self._startup_detail or "ready",
            "startup_ms": round(elapsed_ms, 1),
            "functions": list(self.registered_functions()),
        }

    def ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"result": "pong"}

    def shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.shutdown_requested = True
        return {"status": "shutting_down"}

    # ------------------------------------------------------------------ startup
    def _run_startup_sequence(self) -> None:
        """Exactly one capability detection pass + one telemetry read (Req 16.1/16.2)."""
        started = self._clock()
        status = self.services.client.detect()
        if (self._clock() - started) > 3.0:  # pragma: no cover - timing-guard
            self._startup_status = "degraded"
            self._startup_detail = "startup exceeded the 3s budget"
            return
        if status.value != "ok":
            self._startup_status = "degraded"
            self._startup_detail = (
                f"MSI Afterburner is not available ({status.value}). Monitoring and "
                "diagnostics report it as unavailable until Afterburner is running."
            )
            return
        try:
            self.services.telemetry.get_status(0)
            self._startup_status = "ready"
            self._startup_detail = ""
        except PluginError as exc:
            self._startup_status = "degraded"
            self._startup_detail = f"{exc.user_message} Monitoring continues."
        except Exception:  # pragma: no cover
            self._startup_status = "degraded"
            self._startup_detail = "startup telemetry read failed."

    # ------------------------------------------------------------------ dispatch
    def process(
        self,
        method: str,
        params: Dict[str, Any],
        request_id: Any = None,
    ) -> List[Dict[str, Any]]:
        """Handle one inbound request -> list of wire messages + loop-stop handling."""
        try:
            if method == "initialize":
                return [transport.response(request_id, self.initialize(params))]
            if method == "ping":
                return [transport.response(request_id, self.ping(params))]
            if method == "shutdown":
                result = self.shutdown(params)
                return [transport.response(request_id, result)]
            if method == "execute":
                return self._execute(params, request_id)
            if method == "input":
                return self._handle_input(params, request_id)
            return [
                transport.error(
                    request_id,
                    transport.METHOD_NOT_FOUND,
                    f"Unknown method {method!r}",
                )
            ]
        except PluginError as exc:
            return [self._complete_error(request_id, exc)]
        except Exception as exc:  # pragma: no cover - never escape the loop
            return [
                self._complete_error(
                    request_id,
                    PluginError(
                        ErrorCode.COMM_FAILURE,
                        user_message_for(ErrorCode.COMM_FAILURE),
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )
            ]

    # ------------------------------------------------------------------ execute
    def _execute(
        self, params: Dict[str, Any], request_id: Any
    ) -> List[Dict[str, Any]]:
        function = params.get("function", params.get("name"))
        if not isinstance(function, str) or not function:
            return [
                transport.error(
                    request_id,
                    transport.INVALID_PARAMS,
                    "execute requires a 'function' name",
                )
            ]
        if function not in self._registry:
            # Req 1.7: unknown function -> JSON-RPC error; running state retained.
            return [
                transport.error(
                    request_id,
                    transport.METHOD_NOT_FOUND,
                    f"Function {function!r} is not registered",
                )
            ]
        args, token = self._extract_args(params)
        args.setdefault("__function", function)

        if self.services.policy.requires_confirmation(function):
            return self._risky(function, args, token, request_id)

        executor = self._registry[function]
        return [self._run_executor(executor, args, request_id)]

    @staticmethod
    def _extract_args(params: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
        nested = params.get("params", {})
        if not isinstance(nested, dict):
            nested = {}
        extras = {
            k: v
            for k, v in params.items()
            if k not in ("function", "name", "params", "confirm_token")
        }
        args = {**extras, **nested}
        token = params.get("confirm_token", args.pop("confirm_token", None))
        return args, token

    # ------------------------------------------------------------------ risky
    def _risky(
        self,
        function: str,
        args: Dict[str, Any],
        token: Any,
        request_id: Any,
    ) -> List[Dict[str, Any]]:
        gpu = self._gpu_of_args(args)
        executor = self._registry[function]

        if token is not None:
            # Follow-up carrying the confirm token (preferred path, Req 9.1).
            entry = self.services.policy.validate_token(str(token))
            if entry.function != function:
                raise PluginError(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    "That confirmation doesn't match this request.",
                    detail="token/function mismatch",
                )
            self._pending_input.pop(function, None)
            return [self._run_executor(executor, args, request_id)]

        # Decision-time no-op (Req 19.4): already equal => success, no prompt/write.
        no_op = self._decision_time_noop(function, args, gpu)
        if no_op is not None:
            return [
                self._complete(
                    request_id,
                    message="already applied — no change made",
                    data={"applied": False, "function": function},
                )
            ]

        safe_value = self._safe_prompt_value(function, args, gpu)
        confirm = self.services.policy.issue_confirm_token(function, safe_value)
        self.keep_session = True
        self._pending_input[function] = {
            "args": {k: v for k, v in args.items() if k != "__function"},
            "expires_at": self._clock() + PASSTHROUGH_TIMEOUT_SECONDS,
            "gpu": gpu,
        }
        clause = self._clause(function, gpu, safe_value)
        message = self._prompt_message(function, args, safe_value) + " " + clause
        data = {
            "needs_confirmation": True,
            "confirm_token": confirm.token,
            "function": function,
            "keep_session": True,
            "message": message,
        }
        return [self._complete(request_id, message=message, data=data)]

    # ------------------------------------------------------------------ input
    def _handle_input(
        self, params: Dict[str, Any], request_id: Any
    ) -> List[Dict[str, Any]]:
        function = params.get("function")
        if function is None:
            if len(self._pending_input) == 1:
                function = next(iter(self._pending_input))
            else:
                return [
                    self._complete(
                        request_id,
                        message="No change is currently awaiting confirmation.",
                        data={"needs_confirmation": False},
                    )
                ]
        pending = self._pending_input.get(function)
        if pending is None:
            return [
                self._complete(
                    request_id,
                    message=f"No {function!r} change is awaiting confirmation.",
                    data={"needs_confirmation": False},
                )
            ]
        if self._clock() > pending["expires_at"]:
            self._pending_input.pop(function, None)
            return [
                self._complete(
                    request_id,
                    message="Confirmation timed out — no change was made.",
                    data={"timed_out": True, "function": function},
                )
            ]

        content = params.get("content", params.get("message", ""))
        verdict = self._verdict(content)
        if verdict == "affirmative":
            self._pending_input.pop(function, None)
            executor = self._registry[function]
            args = dict(pending["args"])
            args.setdefault("__function", function)
            return [self._run_executor(executor, args, request_id)]
        if verdict == "negative":
            self._pending_input.pop(function, None)
            return [
                self._complete(
                    request_id,
                    message="Change cancelled — nothing was applied.",
                    data={"cancelled": True, "function": function},
                )
            ]
        return [
            self._complete(
                request_id,
                message="Please reply 'confirm' to apply the change or 'cancel' to stop.",
                data={"needs_confirmation": True, "function": function},
            )
        ]

    # ------------------------------------------------------------------ helpers
    def _gpu_of_args(self, args: Dict[str, Any]) -> int:
        return _gpu_of(args)

    def _decision_time_noop(
        self, function: str, args: Dict[str, Any], gpu: int
    ) -> Optional[float]:
        """Applied state already equals the (clamped) request? -> safe value (or None)."""
        if function not in _SINGLE_VALUE_FEATURE:
            return None
        feature, key = _SINGLE_VALUE_FEATURE[function]
        requested = _require_number(args, key, feature.value)
        safe_value, _clamped = self.services.validator.validate_and_clamp(
            gpu, feature, requested
        )
        current = self.services.ownership.applied_value(gpu, feature)
        if current is not None and abs(current - safe_value) <= self.tolerance:
            return safe_value
        return None

    def _safe_prompt_value(self, function: str, args: Dict[str, Any], gpu: int) -> float:
        """Validate up front so the prompt names the value that would be applied."""
        if function in _SINGLE_VALUE_FEATURE:
            feature, key = _SINGLE_VALUE_FEATURE[function]
            requested = _require_number(args, key, feature.value)
            safe_value, _ = self.services.validator.validate_and_clamp(
                gpu, feature, requested
            )
            return safe_value
        if function == "set_fan_curve":
            self._validate_curve(args, gpu)
            return 0.0
        if function == "optimize_thermal":
            _require_number(args, "target_c", "target temperature")
        return 0.0

    def _validate_curve(self, args: Dict[str, Any], gpu: int) -> None:
        raw_points = args.get("points")
        if not isinstance(raw_points, (list, tuple)) or not raw_points:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                "A fan curve needs a list of [temperature, fan speed] points.",
            )
        try:
            points = tuple(
                FanCurvePoint(temp_c=float(p[0]), fan_percent=float(p[1]))
                for p in raw_points
            )
        except (TypeError, ValueError, IndexError):
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                "Fan curve points must be [temperature, fan speed] pairs.",
            )
        self.services.validator.validate_fan_curve(gpu, FanCurve(points=points))

    def _clause(self, function: str, gpu: int, safe_value: float) -> str:
        feature = _SINGLE_VALUE_FEATURE.get(function)
        if feature is not None:
            return self.services.ownership.ownership_clause(
                gpu, feature[0], requested=safe_value
            )
        return self.services.ownership.ownership_clause(gpu, ControlFeature.FAN_PERCENT)

    def _prompt_message(self, function: str, args: Dict[str, Any], safe: float) -> str:
        if function in _SINGLE_VALUE_FEATURE:
            feature, _ = _SINGLE_VALUE_FEATURE[function]
            label = feature.value.replace("_", " ")
            return f"This will set {label} to {safe:g}. Reply 'confirm' to apply."
        if function == "set_fan_curve":
            n = len(args.get("points") or [])
            return f"This will apply a {n}-point fan curve. Reply 'confirm' to apply."
        return f"This will run {function}. Reply 'confirm' to apply."

    def _run_executor(
        self, executor: Executor, args: Dict[str, Any], request_id: Any
    ) -> Dict[str, Any]:
        gpu = _gpu_of(args)
        try:
            result = executor(args, gpu)
        except PluginError as exc:
            return self._complete_error(request_id, exc)
        except Exception as exc:  # pragma: no cover - never unhandled
            return self._complete_error(
                request_id,
                PluginError(
                    ErrorCode.COMM_FAILURE,
                    user_message_for(ErrorCode.COMM_FAILURE),
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
        return self._complete(request_id, message=result.message, data=result.data)

    # ------------------------------------------------------------------ wire
    @staticmethod
    def _complete(
        request_id: Any, *, message: str, data: Dict[str, Any], success: bool = True
    ) -> Dict[str, Any]:
        params = {"success": success, "message": message, "data": jsonable(data)}
        return transport.notification("complete", params)

    @staticmethod
    def _complete_error(request_id: Any, exc: PluginError) -> Dict[str, Any]:
        params = {
            "success": False,
            "message": exc.user_message,
            "data": jsonable({"error_code": exc.code.value, "detail": exc.detail}),
        }
        return transport.notification("complete", params)

    @staticmethod
    def _verdict(content: Any) -> str:
        text = str(content or "").strip().lower()
        if not text:
            return "unknown"
        first = text.split()[0].strip(".,!?")
        if first in ("confirm", "yes", "y", "accept", "proceed", "ok", "true",
                     "apply", "affirmative", "go"):
            return "affirmative"
        if first in ("no", "n", "cancel", "decline", "deny", "reject", "false",
                     "stop", "abort"):
            return "negative"
        return "unknown"


def _result_data(result: ControlResult) -> Dict[str, Any]:
    return {
        "feature": result.feature.value,
        "applied": result.applied,
        "value": result.applied_value,
        "message": result.message,
    }


def run_plugin_loop(
    plugin: GAssistPlugin,
    reader=None,
    writer=None,
) -> int:
    """Run the protocol message loop over the plugin (injectable streams for tests)."""
    protocol = transport.GAssistProtocol(reader=reader, writer=writer)

    def handler(method, params, request_id):
        messages = plugin.process(method, params, request_id)
        return messages, plugin.shutdown_requested

    protocol.handler = handler
    return protocol.run()


def _state_message(state) -> str:
    parts = []
    if state.power_limit_pct is not None:
        parts.append(f"power limit {state.power_limit_pct:g}%")
    if state.core_offset_mhz is not None:
        parts.append(f"core offset {state.core_offset_mhz:g} MHz")
    if state.memory_offset_mhz is not None:
        parts.append(f"memory offset {state.memory_offset_mhz:g} MHz")
    if state.voltage_mv is not None:
        parts.append(f"voltage {state.voltage_mv:g} mV")
    if state.fan_percent is not None:
        parts.append(f"fan {state.fan_percent:g}% ({state.fan_mode})")
    else:
        parts.append(f"fan mode {state.fan_mode}")
    return "Current tuning: " + ", ".join(parts)


def _parse_profile_ref(raw: Any) -> int:
    """profile_id accepts an int 1..5 or a name like 'Profile 1' / 'profile 1' / '1'."""
    if isinstance(raw, bool):
        raise PluginError(ErrorCode.INVALID_VALUE, "That profile id isn't valid.")
    if isinstance(raw, int):
        if not 1 <= raw <= 5:
            raise PluginError(
                ErrorCode.INVALID_VALUE, f"There's no Afterburner profile {raw}."
            )
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        for prefix in ("profile", "slot", "p"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        try:
            number = int(text)
        except ValueError:
            raise PluginError(
                ErrorCode.INVALID_VALUE, f"There's no Afterburner profile {raw!r}."
            )
        if not 1 <= number <= 5:
            raise PluginError(
                ErrorCode.INVALID_VALUE, f"There's no Afterburner profile {raw!r}."
            )
        return number
    raise PluginError(ErrorCode.INVALID_VALUE, "That profile id isn't valid.")
