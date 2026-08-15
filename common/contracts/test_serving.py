"""Cross-package proofs for the serving configuration input contract."""

from common.contracts.serving import (
    SERVING_CONFIG_INPUTS_FIELDS,
    SERVING_CONFIG_INPUTS_SCHEMA,
)
from common.stage import _serving_config_inputs
from operations.serving.config import CONFIG_INPUTS_SCHEMA, ServingConfigInputs


def test_serving_config_serializer_and_validators_share_one_contract() -> None:
    inputs = ServingConfigInputs("1" * 64, "2" * 64)
    record = inputs.to_record()

    assert set(record) == SERVING_CONFIG_INPUTS_FIELDS
    assert record["schema"] == SERVING_CONFIG_INPUTS_SCHEMA
    assert CONFIG_INPUTS_SCHEMA == SERVING_CONFIG_INPUTS_SCHEMA
    assert ServingConfigInputs.from_record(record) == inputs
    assert _serving_config_inputs(record, "contract test") == record
