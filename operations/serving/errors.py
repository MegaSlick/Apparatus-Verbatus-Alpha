"""Named failures at the serving-manager boundary.

The public outcome of a failed chair start is still the chair framework's
``ServingRecipeRefusal``.  These errors preserve the concrete operational
reason until :mod:`operations.serving.manager` routes it through that existing
no-substitution boundary.
"""

from __future__ import annotations


class ServingError(RuntimeError):
    """Base class for a concrete, non-green serving-manager observation."""

    code = "SERVING_ERROR"


class ServingConfigurationError(ServingError):
    """A serving recipe is incomplete, ambiguous, or unsafe."""

    code = "SERVING_CONFIGURATION_ERROR"


class RuntimePinError(ServingError):
    """The installed runtime differs from the exact declared package pin."""

    code = "VLLM_RUNTIME_PIN_MISMATCH"


class EndpointOccupiedError(ServingError):
    """A loopback endpoint answered before this manager started its process."""

    code = "VLLM_ENDPOINT_OCCUPIED"


class ResidencyError(ServingError):
    """Another manager owns the pod-wide single-resident serving lease."""

    code = "VLLM_RESIDENCY_LOCKED"


class ProcessLaunchError(ServingError):
    """The manager could not create its owned vLLM process."""

    code = "VLLM_LAUNCH_ERROR"


class ReadinessError(ServingError):
    """A bounded readiness transition did not reach a real model answer."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class AdapterActivityError(ServingError):
    """The configured adapter did not produce its required positive evidence."""

    code = "ADAPTER_ACTIVITY_UNPROVEN"


class ReceiptPublicationError(ServingError):
    """Readiness succeeded but the service receipt could not be published."""

    code = "SERVING_RECEIPT_PUBLICATION_FAILED"


class ServiceStopError(ServingError):
    """An owned process could not be stopped and verified as gone."""

    code = "VLLM_STOP_FAILED"
