import re

from lore.query.models import EdgeRow
from lore.query.templates import ROW_FIELDS, TEMPLATES

WRITE_WORDS = ("CREATE", "MERGE", "SET ", "DELETE", "DROP", "ALTER", "COPY")


def test_every_template_is_read_only() -> None:
    for name, template in TEMPLATES.items():
        upper = template.cypher.upper()
        for word in WRITE_WORDS:
            assert word not in upper, f"{name} contains {word!r}"


def test_no_template_interpolates_a_value() -> None:
    # Spec 2.2: every slot binds as a $parameter. An f-string brace or a
    # %s in a template is a value reaching Cypher as text.
    for name, template in TEMPLATES.items():
        assert "{" not in template.cypher, f"{name} has a brace"
        assert "%s" not in template.cypher, f"{name} has a %s"


def test_every_declared_slot_appears_as_a_bound_parameter() -> None:
    for name, template in TEMPLATES.items():
        for slot in template.slots:
            assert f"${slot}" in template.cypher, f"{name} never binds ${slot}"


def test_every_parameter_in_the_cypher_is_a_declared_slot() -> None:
    # The inverse: an unbound $param raises at execution time, and a 3B
    # model filling slots would never know to supply it.
    for name, template in TEMPLATES.items():
        found = set(re.findall(r"\$(\w+)", template.cypher))
        assert found == set(template.slots), f"{name}: {found} != {set(template.slots)}"


def test_every_template_returns_the_uniform_row_shape() -> None:
    # Assembly never branches on which template ran, so every RETURN must
    # alias to the same field names in the same order.
    for name, template in TEMPLATES.items():
        for field in ROW_FIELDS:
            assert f"AS {field}" in template.cypher, f"{name} lacks AS {field}"


def test_row_fields_match_the_edge_row_dataclass() -> None:
    assert ROW_FIELDS == tuple(EdgeRow.__dataclass_fields__)


def test_every_template_is_bounded() -> None:
    for name, template in TEMPLATES.items():
        assert "LIMIT" in template.cypher.upper(), f"{name} is unbounded"


def test_every_template_has_a_description_for_the_model() -> None:
    for name, template in TEMPLATES.items():
        assert len(template.description) > 20, f"{name} description too thin"
        assert template.name == name
