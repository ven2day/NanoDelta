from datetime import UTC, datetime

import pytest

from src.core.models import Market
from src.core.pipeline import (
    LayerBoundaryError,
    LayerEnvelope,
    LayerLineage,
    PipelineLayer,
    ProcessingRole,
    assert_transition,
    role_boundary,
)


def _lineage() -> LayerLineage:
    now = datetime.now(UTC)
    return LayerLineage(
        market=Market.NSE,
        provider="DHAN",
        symbol="RELIANCE",
        event_timestamp=now,
        received_timestamp=now,
        correlation_id="cycle-1:RELIANCE:5m",
    )


def test_happy_path_advances_through_all_six_layers() -> None:
    envelope: LayerEnvelope[object] = LayerEnvelope(
        PipelineLayer.RAW,
        _lineage(),
        {"provider_payload": True},
    )
    for layer in (
        PipelineLayer.CANONICAL,
        PipelineLayer.FEATURE,
        PipelineLayer.DECISION,
        PipelineLayer.EXECUTION,
        PipelineLayer.OUTCOME,
    ):
        envelope = envelope.advance(layer, {"layer": layer.value})

    assert envelope.layer is PipelineLayer.OUTCOME
    assert envelope.lineage.market is Market.NSE
    assert envelope.lineage.correlation_id == "cycle-1:RELIANCE:5m"


def test_layer_skipping_is_rejected() -> None:
    with pytest.raises(LayerBoundaryError, match="RAW -> DECISION"):
        assert_transition(PipelineLayer.RAW, PipelineLayer.DECISION)


def test_outcome_feedback_can_only_return_to_feature_processing() -> None:
    assert_transition(PipelineLayer.OUTCOME, PipelineLayer.FEATURE)
    with pytest.raises(LayerBoundaryError, match="OUTCOME -> EXECUTION"):
        assert_transition(PipelineLayer.OUTCOME, PipelineLayer.EXECUTION)


def test_lineage_requires_timezone_aware_timestamps() -> None:
    with pytest.raises(LayerBoundaryError, match="timezone-aware"):
        LayerLineage(
            market=Market.FOREX,
            provider="OANDA",
            symbol="EUR_USD",
            event_timestamp=datetime.now(),
            received_timestamp=datetime.now(UTC),
            correlation_id="cycle-2:EUR_USD:15m",
        )


def test_ml_and_agents_write_evidence_only_to_decision() -> None:
    ml = role_boundary(ProcessingRole.ML_INFERENCE)
    agents = role_boundary(ProcessingRole.TRADING_AGENTS)

    assert ml.may_read(PipelineLayer.FEATURE)
    assert ml.writes == frozenset({PipelineLayer.DECISION})
    assert agents.may_read(PipelineLayer.FEATURE)
    assert agents.may_read(PipelineLayer.DECISION)
    assert agents.writes == frozenset({PipelineLayer.DECISION})
    assert not agents.may_write(PipelineLayer.EXECUTION)


def test_only_execution_engine_writes_execution_layer() -> None:
    for role in ProcessingRole:
        boundary = role_boundary(role)
        if role is ProcessingRole.EXECUTION_ENGINE:
            assert boundary.may_write(PipelineLayer.EXECUTION)
        else:
            assert not boundary.may_write(PipelineLayer.EXECUTION)


def test_ml_training_reads_features_and_outcomes_but_writes_no_layer() -> None:
    training = role_boundary(ProcessingRole.ML_TRAINING)

    assert training.reads == frozenset({PipelineLayer.FEATURE, PipelineLayer.OUTCOME})
    assert training.writes == frozenset()
