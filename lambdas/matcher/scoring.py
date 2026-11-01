"""
Rules-based scoring for the Matcher (version 2).

The scorer reads the whole job description, not just the title, because the
things that rule a job out (years required, time zone, hybrid only, required
coding) are almost always in the body text.

Order of work:
  1. Title exclusions: titles that are never a fit drop the job at once.
  2. Hard fails: rules in the body (time zone, US only, hybrid away from home)
     drop the job no matter how good the rest looks.
  3. Points: title tier, location, years required, skills, red flags, salary,
     recency.

Every rule that fires is written to a breakdown, so a score is always
explainable. A later version can swap this for an AI model and keep the same
output shape.
"""

import json
import os
import re
from datetime import datetime, timezone

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "scoring.json")


def load_config(path=_CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def _has(text, word):
    """Whole-word match, so "vp" does not match "mvp"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _any(text, words):
    return any(_has(text, w) for w in words)


def _found(text, words):
    return [w for w in words if _has(text, w)]


def _normalise(text):
    return re.sub(r"\s+", " ", (text or "").lower())


# ---------------------------------------------------------------- location
_COUNTRY_WORDS = {"canada", "canadian"}


def _province_code(location, config):
    codes = "|".join(config["province_codes"])
    return re.search(rf",\s*({codes})\b", location) is not None


def _in_canada(location, config):
    return _any(location.lower(), config["canada_keywords"]) or _province_code(location, config)


def _work_mode(lowered, config):
    if _any(lowered, config["remote_keywords"]):
        return "remote"  # "Remote or Onsite" counts as remote: remote is allowed
    if _any(lowered, config["hybrid_keywords"]):
        return "hybrid"
    if _any(lowered, config["onsite_keywords"]):
        return "onsite"
    return "unknown"


def _location_points(location, config):
    """Where and how the job is worked. Returns (points, phrase, flags).

    Preferences: remote anywhere in Canada, hybrid or remote in the home city,
    or remote across North America. A Canadian city that is not the home city
    only counts if the job is remote, because otherwise it means moving. That
    case comes back with the "not_remote" flag and the caller drops the job."""
    pts = config["location_points"]
    if not location.strip():
        return 0, "no location listed", ["location_unknown"]

    lowered = location.lower()
    mode = _work_mode(lowered, config)

    if _in_canada(location, config):
        home = next((c for c in config["home_cities"] if _has(lowered, c)), None)
        named_place = _province_code(location, config) or _any(
            lowered, [w for w in config["canada_keywords"] if w not in _COUNTRY_WORDS])
        if mode == "remote":
            return pts["canada_remote"], "remote in Canada", []
        if home:
            city = home.title()
            if mode == "hybrid":
                return pts["home_hybrid"], f"hybrid in {city}", []
            if mode == "onsite":
                return pts["home_onsite"], f"on-site in {city}, not hybrid or remote", []
            return pts["home_mode_unknown"], f"{city}, work mode not listed, check if hybrid", ["check_work_mode"]
        if not named_place:
            return pts["canada_wide"], "Canada-wide listing, confirm it is remote", ["check_work_mode"]
        return 0, "Canadian city but not listed as remote, would mean relocating", ["not_remote"]

    if mode == "remote" and _any(lowered, config["north_america_keywords"]):
        return pts["north_america_remote"], "remote across North America", []
    if mode == "remote":
        vague = lowered.strip() in {"remote", "remote only", "anywhere", "worldwide", "global"}
        phrase = ("remote, region not stated, confirm Canadians can apply" if vague
                  else "remote outside Canada, confirm Canadians can apply")
        return pts["remote_other"], phrase, ["check_canada_eligibility"]
    return 0, "location unclear", []


# ------------------------------------------------------------------- body
def extract_years(description, config=CONFIG):
    """The highest 'N years' figure that sits near the words experience or
    required, or None. '5-7 years' counts as 7, '2+ years' as 2."""
    rule = config["years_required"]
    best = None
    for m in re.finditer(rule["extract_pattern"], description):
        n = int(m.group(1))
        if n > 30:
            continue
        w = rule["window_chars"]
        window = description[max(0, m.start() - w): m.end() + w]
        if any(word in window for word in rule["context_words"]):
            best = n if best is None else max(best, n)
    return best


def _years_points(years, config):
    if years is None:
        return 0
    for rule in config["years_required"]["rules"]:
        low = rule.get("min_years", 0)
        high = rule.get("max_years", 10 ** 6)
        if low <= years <= high:
            return rule["points"]
    return 0


_MONEY = re.compile(r"(?:\$|cad\s?|usd\s?)\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?k)\b", re.I)


def extract_salary_max(description, config=CONFIG):
    """The largest plausible yearly figure written like $120,000 or $120k."""
    low = config["salary"]["min_plausible"]
    high = config["salary"]["max_plausible"]
    amounts = []
    for m in _MONEY.finditer(description):
        raw = m.group(1).lower().replace(",", "").replace(" ", "")
        value = float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw)
        if low <= value <= high:
            amounts.append(int(value))
    return max(amounts) if amounts else None


def _days_since(posted_at, now):
    text = str(posted_at or "").strip()
    if not text:
        return None
    try:
        if text.isdigit():
            seconds = int(text) / 1000 if len(text) >= 12 else int(text)
            when = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return (now - when).days


def _hard_fail(body, location, config):
    """Returns a reason string if the body or location rules the job out."""
    rules = config["hard_fail"]
    if body:
        timezone_hits = _found(body, rules["body_contains"])
        if timezone_hits and not _any(body, rules["override_if_body_contains"]):
            return f"body says '{timezone_hits[0]}', which rules out working from Alberta"
        hybrid_hits = _found(body, rules["hybrid_keywords"])
        home_listed = any(_has(body, c) or _has(location.lower(), c) for c in config["home_cities"])
        if hybrid_hits and not home_listed and not _any(body, rules["hybrid_override_if_body_contains"]):
            return f"body says '{hybrid_hits[0]}' and Calgary is not listed"
    return None


def _title_tier(title, years, config):
    """Best matching title tier: (points, keyword, tier name) or None."""
    tiers = config["titles"]
    for name, tier in (("core", tiers["core_match"]), ("adjacent", tiers["adjacent_match"]),
                       ("weak", tiers["weak_match"])):
        kws = _found(title, tier["keywords"])
        if not kws:
            continue
        if name == "weak" and years is not None and years > tier["max_years"]:
            continue
        return tier["points"], kws[0], name
    return None


def _drop(reason, flags=None):
    return {"dropped": True, "score": 0, "reason": f"Dropped: {reason}.", "red_flags": [],
            "flags": flags or [], "breakdown": [{"rule": reason, "points": 0}],
            "years_required": None, "work_type": "Not listed", "salary_max": None,
            "low_confidence": False}


def score_posting(posting, config=CONFIG, now=None):
    """Scores one posting. Returns a dict:
    dropped, score (0-100), reason, red_flags, flags, breakdown,
    years_required, work_type, salary_max, low_confidence."""
    now = now or datetime.now(timezone.utc)
    title = _normalise(posting.get("title"))
    location = posting.get("location") or ""
    description = _normalise(posting.get("description"))
    low_confidence = len(description) < config["low_confidence_below_chars"]

    # 1. Titles that are never a fit
    excluded = _found(title, config["titles"]["exclude_if_title_contains"])
    if excluded:
        return _drop(f"'{excluded[0]}' in the title is not a target role")

    # 2. Hard fails
    loc_pts, loc_phrase, loc_flags = _location_points(location, config)
    if "not_remote" in loc_flags:
        return _drop(loc_phrase, loc_flags)
    failure = _hard_fail(description, location, config)
    if failure:
        return _drop(failure)

    # 3. Points
    breakdown, signals = [], []

    years = extract_years(description, config)
    tier = _title_tier(title, years, config)
    if tier:
        pts, kw, name = tier
        breakdown.append({"rule": f"title '{kw}' ({name} match)", "points": pts})
        signals.append(f"title matches '{kw}'" if name != "weak" else f"title has '{kw}', a weak match")
    else:
        signals.append("title is not a close match")

    breakdown.append({"rule": loc_phrase, "points": loc_pts})
    signals.append(loc_phrase)

    years_pts = _years_points(years, config)
    if years is not None:
        breakdown.append({"rule": f"{years} years required", "points": years_pts})
        signals.append(f"{years} years required")

    skills = _found(f"{title} {description}", config["skill_bonus"]["keywords"])
    skill_pts = min(config["skill_bonus"]["cap"], len(skills) * config["skill_bonus"]["points_each"])
    if skills:
        breakdown.append({"rule": f"skills mentioned: {', '.join(skills)}", "points": skill_pts})
        signals.append(f"skills: {', '.join(skills[:6])}")

    red_flags = _found(description, config["red_flags"]["keywords"])
    for flag in red_flags:
        breakdown.append({"rule": f"red flag '{flag}'", "points": config["red_flags"]["points_each"]})

    salary_max = extract_salary_max(description, config)
    salary_pts = 0
    if salary_max is not None:
        if salary_max < config["salary"]["below"]["amount"]:
            salary_pts = config["salary"]["below"]["points"]
        elif salary_max >= config["salary"]["at_or_above"]["amount"]:
            salary_pts = config["salary"]["at_or_above"]["points"]
        if salary_pts:
            breakdown.append({"rule": f"salary up to ${salary_max:,}", "points": salary_pts})

    age = _days_since(posting.get("posted_at"), now)
    if age is not None and age <= config["recency"]["posted_within_days"]:
        breakdown.append({"rule": f"posted {max(age, 0)} days ago", "points": config["recency"]["points"]})

    score = max(0, min(100, sum(item["points"] for item in breakdown)))

    flags = list(loc_flags)
    if low_confidence:
        flags.append("low_confidence")

    mode = _work_mode(location.lower(), config)
    if mode == "unknown":
        mode = _work_mode(description, config)
    work_type = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(mode, "Not listed")

    reason = signals[0].capitalize() + "".join(f"; {s}" for s in signals[1:]) + "."
    return {"dropped": False, "score": score, "reason": reason, "red_flags": red_flags,
            "flags": flags, "breakdown": breakdown, "years_required": years,
            "work_type": work_type, "salary_max": salary_max, "low_confidence": low_confidence}
