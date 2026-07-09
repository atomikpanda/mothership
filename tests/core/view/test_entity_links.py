from mship.core.view.entity_links import linkify_entities

SETS = dict(item_ids={"wi-20260703022747-5b70a0d2"},
            spec_ids={"gc31", "mos-212-chat"},
            task_slugs={"gc31", "mos-212-chat", "mos-224"})

def L(text): return linkify_entities(text, **SETS)

def test_links_wi_spec_task():
    assert L("see wi-20260703022747-5b70a0d2 now") == \
        "see [wi-20260703022747-5b70a0d2](groundcontrol://item?id=wi-20260703022747-5b70a0d2) now"
    assert L("the mos-224 task") == "the [mos-224](groundcontrol://task?id=mos-224) task"

def test_precedence_item_gt_spec_gt_task():
    assert L("check gc31") == "check [gc31](groundcontrol://spec?id=gc31)"  # spec beats task

def test_skips_existing_link_and_code():
    assert L("[gc31](groundcontrol://spec?id=gc31)") == "[gc31](groundcontrol://spec?id=gc31)"
    assert L("run `mos-224` here") == "run `mos-224` here"
    assert L("```\nmos-224\n```") == "```\nmos-224\n```"

def test_unknown_and_substring_untouched():
    assert L("nope-999 and mos-2240 stay") == "nope-999 and mos-2240 stay"

def test_idempotent():
    once = L("gc31 and gc31")
    assert once == "[gc31](groundcontrol://spec?id=gc31) and [gc31](groundcontrol://spec?id=gc31)"
    assert L(once) == once

def test_trailing_punctuation_not_swallowed():
    assert L("see gc31, and mos-224.") == \
        "see [gc31](groundcontrol://spec?id=gc31), and [mos-224](groundcontrol://task?id=mos-224)."
    assert L("(gc31)") == "([gc31](groundcontrol://spec?id=gc31))"

def test_longer_token_containing_ref_untouched():
    assert L("gc31-x not gc31") == "gc31-x not [gc31](groundcontrol://spec?id=gc31)"

def test_empty_text():
    assert L("") == ""
