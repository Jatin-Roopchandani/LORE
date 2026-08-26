import pytest
from pydantic import ValidationError

from lore.extract.schemas import (
    SCHEMA_VERSION,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedTriplet,
)


def test_valid_extraction_parses() -> None:
    payload = {
        "entities": [{"name": "Elizabeth Bennet", "kind": "Character"}],
        "triplets": [
            {
                "subject": "Elizabeth Bennet",
                "predicate": "OPPOSES",
                "object": "Mr Darcy",
                "evidence": "She could not think of Darcy without anger.",
                "confidence": 0.8,
            }
        ],
    }
    extraction = ChunkExtraction.model_validate(payload)
    assert extraction.entities[0].kind == "Character"
    assert extraction.triplets[0].confidence == 0.8


def test_unknown_entity_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({"name": "X", "kind": "Vehicle"})


def test_confidence_must_be_a_probability() -> None:
    base = {
        "subject": "A",
        "predicate": "KNOWS",
        "object": "B",
        "evidence": "text",
    }
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            ExtractedTriplet.model_validate({**base, "confidence": bad})


def test_empty_names_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({"name": "  ", "kind": "Character"})


def test_empty_lists_are_valid() -> None:
    # A chunk of pure scene-setting legitimately yields nothing.
    assert ChunkExtraction.model_validate({"entities": [], "triplets": []}).triplets == []


def test_json_schema_is_guided_decoding_ready() -> None:
    schema = ChunkExtraction.model_json_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"entities", "triplets"}


def test_schema_version_is_recorded() -> None:
    assert SCHEMA_VERSION == "2a.1"
