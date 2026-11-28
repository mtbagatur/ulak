import time

import handoff


def test_settings_fall_back_and_warn():
    s = handoff.settings({"auto_handoff": {"default": True, "min_tokens": "çok", "grace_seconds": 5, "write_prompt": "write a note", "continue_prompt": "/clear"}})
    assert (s["default"], s["min_tokens"], s["grace_seconds"]) == (True, 80000, 600.0)
    assert s["write_prompt"] == handoff.DEFAULT_WRITE and s["continue_prompt"] == handoff.DEFAULT_CONTINUE and len(s["warnings"]) == 2
    assert handoff.settings({})["default"] is False and handoff.settings({"auto_handoff": []})["min_tokens"] == 80000
    own = handoff.settings({"auto_handoff": {"min_tokens": 120000, "grace_seconds": 300, "write_prompt": "Not yaz: ULAK-DEVIR: yazildi ya da ULAK-DEVIR: kesme <neden>", "continue_prompt": "devam"}})
    assert (own["min_tokens"], own["grace_seconds"], own["continue_prompt"], own["warnings"]) == (120000, 300.0, "devam", []) and own["write_prompt"].startswith("Not yaz")
    assert handoff.settings({"auto_handoff": {"continue_prompt": "a\nb"}})["continue_prompt"] == handoff.DEFAULT_CONTINUE


def test_default_prompts_are_one_line_and_carry_both_marks():
    assert handoff.WRITTEN in handoff.DEFAULT_WRITE and handoff.REFUSED in handoff.DEFAULT_WRITE
    assert "\n" not in handoff.DEFAULT_WRITE + handoff.DEFAULT_CONTINUE and handoff.settings({})["warnings"] == []


def test_mark_is_only_the_last_line_of_the_message():
    assert handoff.read_mark("Not yazıldı.\n\nULAK-DEVIR: yazildi\n\n") == ("written", "")
    assert handoff.read_mark("ULAK-DEVIR: kesme  yarım bir\takıl yürütme var") == ("refused", "yarım bir akıl yürütme var")
    assert handoff.read_mark("ULAK-DEVIR: kesme") == ("refused", "")
    assert handoff.read_mark("ULAK-DEVIR: yazildi\nBir şey daha.") is None
    assert handoff.read_mark("> `ULAK-DEVIR: yazildi` yazacağım") is None and handoff.read_mark("ULAK-DEVIR: yazildi!") is None and handoff.read_mark("") is None
    assert handoff.read_mark("ULAK-DEVIR: kesmeyin") is None


def test_reason_is_one_plain_line_of_200():
    kind, why = handoff.read_mark("ULAK-DEVIR: kesme \x1b[31m<script>x</script>\x07" + "a" * 5000)
    assert kind == "refused" and len(why) == 200 and "\x1b" not in why and "\x07" not in why and why.startswith("<script>")


RULE = "─" * 60


def test_box_is_empty_only_in_the_one_known_shape():
    foot = f"{RULE}\n  Fable 5.2 | ctx 5%\n  ⏸ manual mode on\n"
    assert handoff.box_empty(f"● hazir\n{RULE}\n❯\n{foot}") and handoff.box_empty(f"{'─' * 40} ulak devir 22:40 ─\n❯ \n{foot}")
    for full in (f"{RULE}\n❯ yarım cümle\n{foot}", f"{RULE}\n❯ bir\n  iki\n{foot}", f"● hazir\n❯\n{foot}", "", "❯"):
        assert not handoff.box_empty(full)


def test_session_name_keeps_letters_digits_dash_and_underscore():
    at = time.mktime((2026, 11, 28, 22, 40, 0, 0, 0, -1))
    assert handoff.session_name("/home/x/ulak", at) == "ulak devir 22:40"
    assert handoff.session_name("/home/x/a b;rm -rf $(x)", at) == "abrm-rfx devir 22:40" and handoff.session_name("/", at) == "session devir 22:40"
    assert handoff.session_name("/home/x/" + "a" * 90, at) == "a" * 40 + " devir 22:40"
