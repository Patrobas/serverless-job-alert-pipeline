"""
Rules-based scoring for the Matcher.

Every point comes from a visible rule in scoring.json, and every rule that
fires adds a phrase to the reason. That makes each score explainable. V2
swaps this for an AI model, but keeps the same output: a score and a reason.
"""

import json
import os
import re

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "scoring.json")


def load_config(path=_CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def _has(text, word):
    """Whole-word match, so "vp" does not match "mvp"."""
    return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None


_COUNTRY_WORDS = {"canada", "canadian"}


def _any(text, words):
    return any(_has(text, w) for w in words)


def _province_code(location, config):
    codes = "|".join(config["province_codes"])
    return re.search(rf",\s*({codes})\b", location) is not None


def _in_canada(location, config):
    return _any(location.lower(), config["canada_keywords"]) or _province_code(location, config)


def _location_points(location, config):
    """Where and how the job is worked. Returns (points, phrase, flags).

    Preferences: remote anywhere in Canada, hybrid or remote in the home city,
    or remote across North America. A Canadian city that is not the home city
    only counts if the job is remote, because otherwise it means moving."""
    pts = config["location_points"]
    if not location.strip():
        return 0, "no location listed", ["location_unknown"]

    lowered = location.lower()
    if _any(lowered, config["remote_keywords"]):
        mode = "remote"  # "Remote or Onsite" counts as remote: remote is allowed
    elif _any(lowered, config["hybrid_keywords"]):
        mode = "hybrid"
    elif _any(lowered, config["onsite_keywords"]):
        mode = "onsite"
    else:
        mode = "unknown"

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
        return pts["canada_city_not_remote"], "Canadian city but not listed as remote, would mean relocating", ["not_remote"]

    if mode == "remote" and _any(lowered, config["north_america_keywords"]):
        return pts["north_america_remote"], "remote across North America", []
    if mode == "remote":
        return pts["remote_other"], "remote outside Canada, confirm Canadians can apply", ["check_canada_eligibility"]
    return 0, "location unclear", []


def score_posting(posting, config=CONFIG):
    """Returns (score 0-100, reason text, list of flags)."""
    title = (posting.get("title") or "").lower()
    location = posting.get("location") or ""
    company = (posting.get("company") or "").lower()

    score = config["base_points"]
    parts = []
    flags = []

    # Title: the single best matching keyword counts, so a title with
    # several keywords is not rewarded twice.
    matches = [(pts, kw) for kw, pts in config["title_points"].items() if _has(title, kw)]
    if matches:
        pts, kw = max(matches)
        score += pts
        parts.append(f"title matches '{kw}'")
    else:
        parts.append("title is not a close match")

    # Penalties stack: "Director, Pre-sales" is a double step away from the target.
    for kw, pts in config["title_penalty"].items():
        if _has(title, kw):
            score += pts
            parts.append(f"'{kw}' in the title is a step away from your target")

    # Location and work mode
    loc_pts, loc_phrase, loc_flags = _location_points(location, config)
    score += loc_pts
    parts.append(loc_phrase)
    flags.extend(loc_flags)

    bonus = config["company_bonus"].get(company, 0)
    if bonus:
        score += bonus
        parts.append(f"{posting.get('company')} is a priority company")

    score = max(0, min(100, score))
    reason = parts[0].capitalize() + "".join(f"; {p}" for p in parts[1:]) + "."
    return score, reason, flags
