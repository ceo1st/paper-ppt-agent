from backend.generator.template_import.placeholder_schema import validate_actions
from backend.generator.template_import.types import ElementAction


def test_group_placeholder_is_rejected() -> None:
    actions = [
        ElementAction(
            page_type="cover",
            element_id="meta",
            action="replace_with_placeholder",
            placeholder="GROUP",
        )
    ]

    legal, violations = validate_actions(actions, page_type_for_slide={})

    assert legal == []
    assert len(violations) == 1
    assert violations[0].placeholder == "GROUP"
    assert violations[0].reason == "unknown_name"


def test_cover_author_and_date_placeholders_remain_allowed() -> None:
    actions = [
        ElementAction(
            page_type="cover",
            element_id="author",
            action="replace_with_placeholder",
            placeholder="AUTHOR",
        ),
        ElementAction(
            page_type="cover",
            element_id="date",
            action="replace_with_placeholder",
            placeholder="DATE",
        ),
    ]

    legal, violations = validate_actions(actions, page_type_for_slide={})

    assert violations == []
    assert [action.placeholder for action in legal] == ["AUTHOR", "DATE"]
