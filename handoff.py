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
