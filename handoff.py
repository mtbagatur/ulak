"""Automatic handoff: what Ulak may type into an idle session on its own, and when.

Everything here decides and nothing here acts. The app gathers what a session shows, asks `step`, and types only the fixed
texts kept here or in the settings. Nothing an agent wrote or a screen showed ever becomes part of what is typed."""
import re
import time
from pathlib import PurePath

from chat import strip_ansi

MARK = "ULAK-DEVIR:"
WRITTEN = "ULAK-DEVIR: yazildi"
REFUSED = "ULAK-DEVIR: kesme"
LEAD = 600          # seconds before the cache runs out at which the note is asked for
ASK_TIMEOUT = 300   # how long the note may take
REASON_MAX = 200
MIN_TOKENS = 80_000  # a smaller conversation is cheap to read cold; cutting it costs more than it saves
GRACE = 600.0
PROMPT_MAX = 2000

DEFAULT_WRITE = ("Ulak auto handoff: the prompt cache is about to run out and this session is idle. If cutting the conversation now would lower "
                 "the quality of the work (reasoning left half way, context that will not fit in a note, an answer you are waiting for), write no note; "
                 f"give the reason in one sentence on this line and stop: `{REFUSED} <reason>`. Otherwise write or update the handoff note the way this "
                 f"project's rules say, do no other work, and make this your last line: `{WRITTEN}`.")
DEFAULT_CONTINUE = ("continue (Ulak auto handoff: read the newest handoff note and carry on from the next step. Leave steps that wait for approval alone; "
                    "if nothing is left that may go on without approval, say so in one line and stop.)")

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _number(value, low, high, default, kind):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        return default
    return kind(value)


def _prompt(value, default, name, warnings, needs=()):
    if value is None:
        return default
    why = ("is not text" if not isinstance(value, str) else "is empty" if not value.strip() else f"is longer than {PROMPT_MAX} characters" if len(value) > PROMPT_MAX
           else "has a line break or a control character" if CONTROL.search(value) else "starts with a slash" if value.lstrip().startswith("/")
           else "names neither mark the agent must answer with" if any(mark not in value for mark in needs) else "")
    if why:
        warnings.append(f"{name} {why}; the built-in text is used")
        return default
    return value.strip()


def settings(config):
    """The `auto_handoff` part of the local config, with every value that cannot be used replaced by its default."""
    own = config.get("auto_handoff") if isinstance(config, dict) else None
    own = own if isinstance(own, dict) else {}
    warnings = []
    return {"default": own.get("default") is True,
            "min_tokens": _number(own.get("min_tokens"), 1000, 10**9, MIN_TOKENS, int),
            "grace_seconds": _number(own.get("grace_seconds"), 60, 3600, GRACE, float),
            "write_prompt": _prompt(own.get("write_prompt"), DEFAULT_WRITE, "write_prompt", warnings, (WRITTEN, REFUSED)),
            "continue_prompt": _prompt(own.get("continue_prompt"), DEFAULT_CONTINUE, "continue_prompt", warnings),
            "warnings": warnings}


def read_mark(text):
    """What the last line of an agent's answer says: ("written", ""), ("refused", reason), or None when it is no mark at all."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last == WRITTEN:
        return "written", ""
    if last == REFUSED or (last.startswith(REFUSED) and last[len(REFUSED)].isspace()):
        reason = " ".join(CONTROL.sub(" ", strip_ansi(last[len(REFUSED):])).split())
        return "refused", reason[:REASON_MAX]
    return None


def _rule(line, whole):
    line = line.strip()
    return line.count("─") >= 10 and (set(line) <= {"─", " "} if whole else line.startswith("─" * 10))


def box_empty(screen):
    """True only when the end of the screen is Claude Code's input box with nothing typed in it. Anything unfamiliar counts as full."""
    lines = str(screen or "").splitlines()
    for i in range(len(lines) - 1, 1, -1):
        if _rule(lines[i], True):
            return lines[i - 1].strip() == "❯" and _rule(lines[i - 2], False)
    return False


def session_name(cwd, now):
    """The name a cleared conversation is given, made only of parts Ulak knows itself."""
    project = "".join(c for c in PurePath(str(cwd or "")).name if c.isalnum() or c in "-_")[:40] or "session"
    return f"{project} devir {time.strftime('%H:%M', time.localtime(now))}"


def fresh(on, born, conversation):
    """A session nothing has been asked of yet."""
    return {"on": bool(on), "born": born, "phase": "armed", "conversation": conversation, "asked_at": 0.0, "ask_at": -1, "mark_at": -1, "due": 0.0}


def quiet(view):
    """Nobody is there and nothing is under way: an idle Claude session with no question up, no call or subagent out and an empty input box.
    A measure that could not be read counts as someone being there."""
    return (view["agent"] == "claude" and view["status"] in ("idle", "done") and not view["prompt"] and view["calls"] == 0
            and view["agents"] == 0 and view["box_empty"] is True)


def can_clean(st, view):
    """The note Ulak asked for is still the last word of the same conversation, and nobody has come back."""
    return bool(st["on"] and st["phase"] == "written" and quiet(view) and view["conversation"] == st["conversation"]
                and view["user_at"] == st["ask_at"] and view["assistant_at"] == st["mark_at"])


def _due_to_ask(view, now, cfg):
    cache, tokens = view["cache"], view["tokens"]
    return bool(cache and cache["ttl"] == 3600 and cache["ttl"] - LEAD <= now - cache["since"] < cache["ttl"]
                and tokens is not None and tokens >= cfg["min_tokens"])


def step(st, view, now, cfg):
    """One look at one session -> (its new state, what to do, a detail).

    What to do: None, "ask" (type the handoff prompt), "written" (tell: the note is there, the wait began), "refused" (tell the reason
    in the detail), "failed" (tell: nothing was written or the session changed), "clean" (clear, rename, continue; the state stays as it is
    until that is done). A state that is `held` is not asked again until a person says something in that conversation."""
    if not st["on"]:
        return fresh(False, st["born"], view["conversation"]), None, ""
    if view["conversation"] != st["conversation"]:  # someone cleared it, or another conversation was resumed
        return fresh(True, st["born"], view["conversation"]), None, ""
    st = dict(st)
    held = lambda: ({**st, "phase": "held", "ask_at": view["user_at"] if st["ask_at"] == -1 else st["ask_at"]}, "failed", "")
    again = lambda: (fresh(True, st["born"], st["conversation"]), None, "")
    phase = st["phase"]
    if phase == "armed":
        if quiet(view) and _due_to_ask(view, now, cfg):
            return {**st, "phase": "asked", "asked_at": now}, "ask", ""
        return st, None, ""
    if phase == "asked":
        late = now - st["asked_at"] > ASK_TIMEOUT
        if st["ask_at"] == -1:
            if view["user_text"] != cfg["write_prompt"]:
                return held() if late else (st, None, "")
            st["ask_at"] = view["user_at"]
        if view["user_at"] != st["ask_at"]:
            return again()  # the person wrote: their turn, not ours
        if view["status"] in ("idle", "done") and not view["prompt"] and view["assistant_at"] != -1:
            mark = read_mark(view["assistant_text"])
            if not mark:
                return held()
            if mark[0] == "refused":
                return {**st, "phase": "held"}, "refused", mark[1]
            return {**st, "phase": "written", "mark_at": view["assistant_at"], "due": now + cfg["grace_seconds"]}, "written", ""
        return held() if late else (st, None, "")
    if phase == "written":
        if view["user_at"] != st["ask_at"] or view["status"] == "working" or view["assistant_at"] != st["mark_at"]:
            return again()
        if now < st["due"]:
            return st, None, ""
        return (st, "clean", "") if can_clean(st, view) else held()
    if st["ask_at"] == -1:  # held before the record showed what was asked: the hold lasts until the next thing a person says
        return {**st, "ask_at": view["user_at"]}, None, ""
    if view["user_at"] != st["ask_at"] and view["user_text"] != cfg["write_prompt"]:
        return again()
    return st, None, ""
