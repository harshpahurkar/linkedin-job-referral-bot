"""
Shared geography helpers for job and contact filtering.
"""

from __future__ import annotations


_CANADA_LOCATION_KEYWORDS = [
    "canada",
    "ontario", ", on", "toronto", "ottawa", "waterloo", "kitchener",
    "mississauga", "hamilton", "london, on", "brampton", "markham",
    "richmond hill", "oakville", "burlington", "vaughan",
    "british columbia", ", bc", "vancouver", "victoria, bc", "burnaby", "surrey",
    "alberta", ", ab", "calgary", "edmonton",
    "manitoba", ", mb", "winnipeg",
    "saskatchewan", ", sk", "saskatoon", "regina",
    "nova scotia", ", ns", "halifax",
    "new brunswick", ", nb", "moncton", "fredericton",
    "newfoundland", ", nl", "st. john's",
    "prince edward island", ", pe", "charlottetown",
    "yukon", "nunavut", "northwest territories",
    "gta", "greater toronto",
    "remote canada", "canada remote",
    "quebec", ", qc", "montreal", "montréal", "québec", "laval",
]

_QUEBEC_LOCATION_KEYWORDS = [
    "quebec", ", qc", "montreal", "montréal", "québec", "laval",
    "longueuil", "gatineau", "brossard", "sherbrooke", "trois-rivieres",
    "trois-rivières", "drummondville", "blainville", "saint-laurent",
]

_PREFERRED_CANADIAN_LOCATION_KEYWORDS = [
    "toronto", "ottawa", "waterloo", "kitchener", "mississauga",
    "hamilton", "markham", "richmond hill", "oakville", "burlington",
    "vaughan", "brampton", "scarborough", "north york", "etobicoke",
    "gta", "greater toronto",
    "vancouver", "burnaby", "surrey", "victoria, bc",
    "calgary", "edmonton", "winnipeg", "halifax",
]

_US_LOCATION_KEYWORDS = [
    "united states", ", us",
    "new york", "san francisco", "seattle", "austin", "chicago",
    "boston", "los angeles", "denver", "atlanta", "dallas",
    "washington", "portland", "philadelphia", "miami",
]


def _blob(text: str | None) -> str:
    return (text or "").strip().lower()


def is_quebec_location(location: str | None) -> bool:
    """True when a location string points to Quebec or Montreal."""
    blob = _blob(location)
    return any(keyword in blob for keyword in _QUEBEC_LOCATION_KEYWORDS)


def is_canadian_location(location: str | None) -> bool:
    """True when a location string looks Canadian."""
    blob = _blob(location)
    return any(keyword in blob for keyword in _CANADA_LOCATION_KEYWORDS)


def is_us_location(location: str | None) -> bool:
    """True when a location string clearly looks US-based."""
    blob = _blob(location)
    return any(keyword in blob for keyword in _US_LOCATION_KEYWORDS)


def is_north_american_location(location: str | None) -> bool:
    """True when a location string looks Canadian or US-based."""
    return is_canadian_location(location) or is_us_location(location) or "north america" in _blob(location)


def is_preferred_canadian_location(location: str | None) -> bool:
    """True for non-Quebec Canadian metro areas we want to prioritize."""
    blob = _blob(location)
    if is_quebec_location(blob):
        return False
    return any(keyword in blob for keyword in _PREFERRED_CANADIAN_LOCATION_KEYWORDS)


def is_remote_location(location: str | None) -> bool:
    """True when the location looks remote/hybrid-friendly."""
    blob = _blob(location)
    return "remote" in blob


def is_remote_or_us_job_location(location: str | None) -> bool:
    """True for US jobs or remote jobs without a clear Canadian signal."""
    blob = _blob(location)
    if not blob:
        return False
    if is_us_location(blob):
        return True
    return is_remote_location(blob) and not is_canadian_location(blob)


def job_location_preference_score(location: str | None) -> float:
    """Return a ranking boost/penalty for job locations."""
    blob = _blob(location)
    if not blob:
        return 0.0
    if is_quebec_location(blob):
        return -100.0
    if is_preferred_canadian_location(blob):
        return 3.5
    if is_canadian_location(blob):
        return 1.5 if is_remote_location(blob) else 1.0
    if is_remote_location(blob):
        return 0.5
    return 0.0


def contact_location_priority(
    location: str | None,
    *,
    allow_north_america: bool = False,
) -> int:
    """Return a priority bucket for contacts by location fit."""
    blob = _blob(location)
    if not blob:
        return 15
    if is_quebec_location(blob):
        return -100
    if is_preferred_canadian_location(blob):
        return 40
    if is_canadian_location(blob):
        return 30
    if allow_north_america and is_us_location(blob):
        return 10
    return 0
