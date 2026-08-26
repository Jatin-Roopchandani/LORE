from lore.extract.prompts import PREFERRED_PREDICATES, build_prompt


def test_prompt_embeds_the_chunk_text() -> None:
    prompt = build_prompt("Elizabeth walked to Netherfield.")
    assert "Elizabeth walked to Netherfield." in prompt


def test_prompt_steers_toward_the_curated_predicates() -> None:
    prompt = build_prompt("text")
    for predicate in ("ALLEGIANCE_TO", "INTERACTS_WITH", "BECOMES"):
        assert predicate in prompt
    assert "ALLEGIANCE_TO" in PREFERRED_PREDICATES


def test_prompt_demands_evidence_quoted_from_the_passage() -> None:
    prompt = build_prompt("text").lower()
    assert "evidence" in prompt
    assert "verbatim" in prompt


def test_prompt_is_deterministic() -> None:
    assert build_prompt("same text") == build_prompt("same text")
