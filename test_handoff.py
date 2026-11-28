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


# ---------- the state machine ----------
CFG = handoff.settings({})
NOW = 1_800_000_000.0
VIEW = {"agent": "claude", "status": "idle", "prompt": False, "calls": 0, "agents": 0, "tokens": 142_000, "cache": {"ttl": 3600, "since": NOW - 3000},
        "box_empty": True, "conversation": "c1", "user_at": 100, "user_text": "şunu yap", "assistant_at": 200, "assistant_text": "yaptım"}


def view(**changes):
    return {**VIEW, **changes}


def armed(on=True):
    return handoff.fresh(on, "born1", "c1")


def asked(now=NOW):
    """Asked, and the record already shows Ulak's own words as the last thing said."""
    st, action, _ = handoff.step(armed(), view(), now, CFG)
    assert action == "ask"
    st, action, _ = handoff.step(st, view(status="working", user_at=300, user_text=CFG["write_prompt"], assistant_at=-1, assistant_text=""), now + 15, CFG)
    assert action is None and st["phase"] == "asked" and st["ask_at"] == 300
    return st


NOTE = dict(user_at=300, user_text=CFG["write_prompt"], assistant_at=400, assistant_text="Not güncel.\n\nULAK-DEVIR: yazildi")


def written(now=NOW):
    st, action, _ = handoff.step(asked(now), view(**NOTE), now + 60, CFG)
    assert action == "written"
    return st


def test_asks_in_the_last_ten_minutes_of_a_one_hour_cache():
    for age, expected in ((2999, None), (3000, "ask"), (3599, "ask"), (3600, None), (9000, None)):
        st, action, _ = handoff.step(armed(), view(cache={"ttl": 3600, "since": NOW - age}), NOW, CFG)
        assert action == expected and st["phase"] == ("asked" if expected else "armed"), age
    assert handoff.step(armed(), view(), NOW, CFG)[0]["asked_at"] == NOW


def test_never_asks_a_five_minute_cache_or_a_small_conversation():
    for change in ({"cache": {"ttl": 300, "since": NOW - 200}}, {"cache": None}, {"tokens": 79_999}, {"tokens": None}):
        assert handoff.step(armed(), view(**change), NOW, CFG)[1] is None, change
    assert handoff.step(armed(), view(tokens=80_000), NOW, CFG)[1] == "ask"


def test_never_asks_while_someone_is_there():
    for change in ({"status": "working"}, {"status": "blocked"}, {"status": "unknown"}, {"prompt": True}, {"calls": 1}, {"calls": None}, {"agents": 1}, {"box_empty": False}, {"agent": "codex"}):
        assert handoff.step(armed(), view(**change), NOW, CFG)[1] is None, change
    assert handoff.step(armed(), view(status="done"), NOW, CFG)[1] == "ask"


def test_off_does_nothing():
    st, action, _ = handoff.step(armed(on=False), view(), NOW, CFG)
    assert action is None and st["phase"] == "armed" and st["on"] is False


def test_the_note_starts_the_ten_minutes():
    st = written()
    assert st["phase"] == "written" and st["due"] == NOW + 60 + 600 and st["mark_at"] == 400 and st["ask_at"] == 300
    assert handoff.step(st, view(**NOTE), NOW + 120, CFG)[1:] == (None, "")


def test_a_refusal_is_told_and_not_asked_again():
    st, action, why = handoff.step(asked(), view(**{**NOTE, "assistant_text": "ULAK-DEVIR: kesme yarım akıl yürütme"}), NOW + 60, CFG)
    assert (action, why, st["phase"]) == ("refused", "yarım akıl yürütme", "held")
    again = view(**{**NOTE, "assistant_text": "ULAK-DEVIR: kesme yarım akıl yürütme", "cache": {"ttl": 3600, "since": NOW + 60}})
    st, action, _ = handoff.step(st, again, NOW + 60 + 3100, CFG)
    assert action is None and st["phase"] == "held"
    back = view(user_at=500, user_text="tamam devam", assistant_at=600, cache={"ttl": 3600, "since": NOW + 4000})
    st, action, _ = handoff.step(st, back, NOW + 4100, CFG)
    assert action is None and st["phase"] == "armed"
    assert handoff.step(st, back, NOW + 4000 + 3100, CFG)[1] == "ask"


def test_no_mark_or_no_answer_in_five_minutes_gives_up():
    st, action, _ = handoff.step(asked(), view(**{**NOTE, "assistant_text": "Notu yazdım."}), NOW + 60, CFG)
    assert (action, st["phase"]) == ("failed", "held")
    busy = view(status="working", user_at=300, user_text=CFG["write_prompt"], assistant_at=-1, assistant_text="")
    assert handoff.step(asked(), busy, NOW + 300, CFG)[1] is None
    assert handoff.step(asked(), busy, NOW + 301, CFG)[1] == "failed"
    st, action, _ = handoff.step(asked(), {**busy, "status": "blocked", "prompt": True}, NOW + 301, CFG)
    assert (action, st["phase"]) == ("failed", "held")
    assert handoff.step(asked(), {**busy, "status": "blocked", "prompt": True}, NOW + 100, CFG)[1] is None


def test_a_prompt_that_never_landed_gives_up():
    st, _, _ = handoff.step(armed(), view(), NOW, CFG)
    assert handoff.step(st, view(), NOW + 60, CFG)[1] is None
    st, action, _ = handoff.step(st, view(), NOW + 301, CFG)
    assert (action, st["phase"]) == ("failed", "held")


def test_writing_while_the_note_is_asked_for_drops_it_quietly():
    st, action, _ = handoff.step(asked(), view(status="working", user_at=350, user_text="dur bir saniye", assistant_at=-1), NOW + 30, CFG)
    assert action is None and st["phase"] == "armed"


def test_writing_during_the_wait_keeps_the_conversation():
    st, action, _ = handoff.step(written(), view(user_at=500, user_text="buradayım", assistant_at=-1, assistant_text=""), NOW + 5000, CFG)
    assert action is None and st["phase"] == "armed"


def test_working_or_a_newer_answer_during_the_wait_keeps_it():
    for change in ({"status": "working"}, {"assistant_at": 450, "assistant_text": "arka plan işi bitti"}):
        st, action, _ = handoff.step(written(), view(**{**NOTE, **change}), NOW + 5000, CFG)
        assert action is None and st["phase"] == "armed", change


def test_cleans_only_when_everything_still_holds():
    st = written()
    assert handoff.step(st, view(**NOTE), st["due"] - 1, CFG)[1] is None
    after, action, _ = handoff.step(st, view(**NOTE), st["due"], CFG)
    assert action == "clean" and after == st and handoff.can_clean(st, view(**NOTE))
    for change in ({"box_empty": False}, {"prompt": True, "status": "blocked"}, {"calls": 1}, {"calls": None}, {"agents": 2}):
        after, action, _ = handoff.step(st, view(**{**NOTE, **change}), st["due"], CFG)
        assert (action, after["phase"]) == ("failed", "held") and not handoff.can_clean(st, view(**{**NOTE, **change})), change
    assert handoff.step(st, view(**{**NOTE, "tokens": 10}), st["due"], CFG)[1] == "clean"  # the size mattered only when asking


def test_turning_it_off_during_the_wait_cleans_nothing():
    st = {**written(), "on": False}
    after, action, _ = handoff.step(st, view(**NOTE), st["due"] + 1, CFG)
    assert action is None and after["phase"] == "armed" and not after["on"] and not handoff.can_clean(st, view(**NOTE))


def test_a_cleared_conversation_starts_over():
    for st in (asked(), written(), {**written(), "phase": "held"}):
        after, action, _ = handoff.step(st, view(conversation="c2", user_at=-1, user_text="", assistant_at=-1, assistant_text=""), NOW + 5000, CFG)
        assert action is None and (after["phase"], after["conversation"], after["ask_at"], after["on"], after["born"]) == ("armed", "c2", -1, True, "born1")


def test_hours_later_the_marked_answer_must_still_be_the_last_word():
    st = written()
    assert handoff.step(st, view(**NOTE), st["due"] + 4 * 3600, CFG)[1] == "clean"
    after, action, _ = handoff.step(st, view(**{**NOTE, "assistant_at": 900, "assistant_text": "özet"}), st["due"] + 4 * 3600, CFG)
    assert action is None and after["phase"] == "armed"


def test_step_leaves_its_input_alone():
    st, v = armed(), view()
    before = (dict(st), dict(v))
    handoff.step(st, v, NOW, CFG)
    assert (st, v) == before


def test_a_hold_set_before_the_prompt_landed_lasts_until_someone_speaks():
    st = {**handoff.step(armed(), view(), NOW, CFG)[0], "phase": "held"}  # Cancel, a moment after asking
    st, action, _ = handoff.step(st, view(), NOW + 20, CFG)
    assert action is None and (st["phase"], st["ask_at"]) == ("held", 100)
    assert handoff.step(st, view(), NOW + 40, CFG)[1] is None
    assert handoff.step(st, view(user_at=700, user_text="geldim"), NOW + 60, CFG)[0]["phase"] == "armed"
