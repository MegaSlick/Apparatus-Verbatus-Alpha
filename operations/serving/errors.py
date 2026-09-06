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


class ChairRequestRefusal(ServingError):
    """A reading request cannot go on the wire as the caller shaped it.

    This is a refusal about the request the caller built — an unsupported
    ``kind``, an OpenAI field a chair may never set, an image whose bytes do
    not match the digest the caller claims to be sending — never about what
    an engine sent back.  ``ChairResponseRefusal`` is that other half.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ChairResponseRefusal(ServingError):
    """An engine's raw HTTP response is not a valid reading for this chair.

    Every ``CHAIR_RESPONSE_*`` code names one specific way a response fails to
    be a reading — non-200, unparseable body, wrong model, not exactly one
    choice, missing content.  None of these retries, re-samples, or edits the
    response: the raw bytes are retained before this is raised, so a caller
    that must keep evidence rather than abort can catch this and record
    ``code`` as ``parse_problem``.

    That retention claim was once false for exactly the two codes raised
    earliest — ``CHAIR_RESPONSE_HTTP_ERROR`` and ``CHAIR_RESPONSE_MODEL_MISMATCH``
    were raised *before* the body was written, so a vLLM 400 explaining a
    context overflow was discarded on a card that bills by the hour. It is true
    now: ``ChairClient.read`` retains before it checks, and both of those
    refusals carry the retained reference in ``detail``. The non-200 also
    carries the head of the body, because that is where the engine's own
    account of its refusal lives; the wrong-model refusal names the blob and
    nothing else, because a 200 from another model is a foreign reading and a
    foreign reading's text does not travel in an exception message.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")
