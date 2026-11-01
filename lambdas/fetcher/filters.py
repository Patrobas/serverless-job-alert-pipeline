"""
Cheap pre-filter for the Fetcher.

Why this exists: later, an AI model (Bedrock) scores each job, and Bedrock
bills per use. Dropping obvious mismatches here, with plain string checks
that cost nothing, means we only pay for AI judgment on jobs worth judging.

This filter is deliberately loose. It removes clear misses (wrong title,
wrong country, an in-person job in the wrong city) and leaves the nuanced
calls, like "remote but US only", to the Matcher.
"""

import json
import os
import re

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.json")


def load_profile(path=_PROFILE_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


PROFILE = load_profile()


def _has_any(text, words):
    """True if any word appears as a whole word or phrase.
    Whole-word matching stops "uk" matching "Milwaukee" and
    "india" matching "Indiana"."""
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def title_ok(title, profile=PROFILE):
    t = (title or "").lower()
    if any(word in t for word in profile["title_exclude"]):
        return False
    return any(word in t for word in profile["title_include"])


def location_ok(location, profile=PROFILE):
    text = (location or "").strip()
    if not text:
        return profile.get("keep_if_location_unknown", True)

    lowered = text.lower()

    # 1. Canada. The rules, in order:
    #    - remote anywhere in Canada: keep
    #    - the home city (any work mode, the Matcher ranks hybrid above on-site): keep
    #    - "Canada" with no city named: keep, work mode unknown
    #    - any other Canadian city or province that is not remote: drop, since
    #      it would mean relocating
    codes = "|".join(profile["province_codes"])
    has_province_code = re.search(rf",\s*({codes})\b", text) is not None  # "Toronto, ON"
    if _has_any(lowered, profile["canada_keywords"]) or has_province_code:
        if _has_any(lowered, profile["remote_keywords"]):
            return True
        if _has_any(lowered, profile.get("home_cities", [])):
            return True
        place_words = [w for w in profile["canada_keywords"] if w not in ("canada", "canadian")]
        return not (has_province_code or _has_any(lowered, place_words))

    # 2. A place outside North America is dropped, unless the posting also
    #    says North America or the United States (e.g. "Remote, US or UK").
    if _has_any(lowered, profile["exclude_locations"]) and not _has_any(
        lowered, profile["override_keywords"]
    ):
        return False

    # 3. Remote with no foreign place named: keep. The Matcher judges whether
    #    a US remote role is open to Canadians.
    return _has_any(lowered, profile["remote_keywords"])


def passes_filters(posting, profile=PROFILE):
    return title_ok(posting["title"], profile) and location_ok(posting["location"], profile)
