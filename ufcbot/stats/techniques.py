"""A shared vocabulary for how fights end.

ufcstats.com and ESPN describe finishes differently ("Punches to Head From Mount"
against "Punches" with a head target). Both are mapped onto one set of labels so
the model can learn from one and be graded against the other.

Neither source separates KO from TKO, so they stay one method.
"""

from __future__ import annotations

METHODS = ("ko", "sub", "dec_u", "dec_s")

METHOD_LABELS = {
    "ko": "KO/TKO",
    "sub": "Submission",
    "dec_u": "Unanimous decision",
    "dec_s": "Split decision",
    "draw": "Draw",
    "nc": "No contest",
}

METHOD_SHORT = {
    "ko": "KO/TKO",
    "sub": "Sub",
    "dec_u": "U-Dec",
    "dec_s": "S-Dec",
    "draw": "Draw",
    "nc": "NC",
}

# Methods that end with a specific technique.
FINISHES = ("ko", "sub")

_GROUND = ("ground", "mount", "guard", "back control", "side control", "north-south", "crucifix")


def method_detail(method: str | None) -> str:
    """ufcstats method text to ko, sub, dec_u, dec_s or other."""
    text = (method or "").lower()
    if "decision" in text:
        if "split" in text or "majority" in text:
            return "dec_s"
        return "dec_u"
    if "submission" in text:
        return "sub"
    if "ko" in text:
        return "ko"
    return "other"


def _strike_technique(text: str, *, ground: bool) -> str:
    if "slam" in text:
        return "slam"
    if "kick" in text:
        if "body" in text:
            return "body kick"
        if "leg" in text:
            return "leg kicks"
        return "head kick"
    if "knee" in text:
        return "knee"
    if "back fist" in text or "backfist" in text:
        return "spinning back fist"
    if "elbow" in text:
        return "ground and pound" if ground else "elbows"
    if "punch" in text:
        if "body" in text:
            return "body punches"
        return "ground and pound" if ground else "punches"
    return "strikes"


def _submission_technique(text: str) -> str:
    ordered = (
        ("rear naked", "rear-naked choke"),
        ("guillotine", "guillotine"),
        ("arm triangle", "arm-triangle"),
        ("arm-triangle", "arm-triangle"),
        ("d'arce", "D'Arce choke"),
        ("darce", "D'Arce choke"),
        ("anaconda", "anaconda choke"),
        ("triangle", "triangle choke"),
        ("armbar", "armbar"),
        ("kimura", "kimura"),
        ("heel hook", "heel hook"),
        ("kneebar", "kneebar"),
        ("neck crank", "neck crank"),
        ("americana", "americana"),
        ("ezekiel", "ezekiel choke"),
        ("north-south", "north-south choke"),
        ("north south", "north-south choke"),
        ("von flue", "von Flue choke"),
        ("calf slicer", "calf slicer"),
        ("peruvian", "Peruvian necktie"),
        ("strikes", "strikes"),
    )
    for needle, label in ordered:
        if needle in text:
            return label
    return "other"


def technique_from_ufcstats(detail: str, method: str | None, details: str | None) -> str | None:
    """Technique label for a finish recorded on ufcstats.com, None for decisions."""
    text = (details or "").lower()
    if detail == "ko":
        method_text = (method or "").lower()
        if "doctor" in method_text:
            return "doctor stoppage"
        if "corner" in text:
            return "corner stoppage"
        return _strike_technique(text, ground=any(word in text for word in _GROUND))
    if detail == "sub":
        return _submission_technique(text)
    return None


def method_from_espn(result_name: str | None, display_name: str | None = None) -> str | None:
    """ESPN result name ("kotko", "submission", "decision---split") to a method."""
    text = f"{result_name or ''} {display_name or ''}".lower()
    if "draw" in text:
        return "draw"
    if "decision" in text:
        if "split" in text or "majority" in text:
            return "dec_s"
        return "dec_u"
    if "submission" in text:
        return "sub"
    if "ko" in text or "doctor" in text:
        return "ko"
    return None


def technique_from_espn(method: str | None, description: str | None, target: str | None) -> str | None:
    """Technique label from ESPN's result description and strike target."""
    text = f"{description or ''} {target or ''}".lower()
    if method == "ko":
        if "doctor" in text or "cut" in text or "injury" in text:
            return "doctor stoppage"
        if "corner" in text:
            return "corner stoppage"
        # ESPN never says where a strike landed from, so ground strikes read as punches.
        return _strike_technique(text, ground=False)
    if method == "sub":
        return _submission_technique(text)
    return None


# Techniques ESPN cannot tell apart are graded as one family.
_FAMILY = {
    "spinning back fist": "punches",
    "ground and pound": "punches",
    "elbows": "punches",
    "body punches": "punches",
    "strikes": "punches",
}


def same_technique(predicted: str | None, actual: str | None) -> bool | None:
    """Whether a predicted technique matches the result, None when either is unknown."""
    if not predicted or not actual or actual in ("other", "strikes"):
        return None
    return _FAMILY.get(predicted, predicted) == _FAMILY.get(actual, actual)


def describe(method: str, technique: str | None = None) -> str:
    """Human wording: "KO/TKO (head kick)", "Submission (rear-naked choke)", "Split decision"."""
    label = METHOD_LABELS.get(method, method)
    if technique and method in FINISHES and technique not in ("other", "strikes"):
        return f"{label} ({technique})"
    return label
