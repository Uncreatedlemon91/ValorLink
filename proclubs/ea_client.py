"""Thin client for EA's unofficial Pro Clubs API (proclubs.ea.com/api/fc).

This API is not publicly documented by EA and has no stability guarantees --
endpoints, fields, and platform codes have changed across FC titles and can
break or go down without notice.
"""

import time

# EA's edge (Akamai Bot Manager) fingerprints the TLS/HTTP handshake and
# blocks plain `requests` traffic with a 403 even though the same URL works
# fine from a real browser. curl_cffi impersonates a real Chrome fingerprint,
# which gets past it.
from curl_cffi import requests

BASE_URL = "https://proclubs.ea.com/api/fc/"

# "common-gen5" covers PC + current-gen consoles (PS5 / Xbox Series) since
# Pro Clubs crossplay pools those together. "common-gen4" is last-gen
# (PS4 / Xbox One). "nx" is a legacy value from older FIFA titles.
PLATFORMS = {
    "common-gen5": "PC / PS5 / Xbox Series",
    "common-gen4": "PS4 / Xbox One",
    "nx": "Legacy",
}

IMPERSONATE = "chrome124"

_TIMEOUT = 10
_CACHE_TTL = 30  # seconds -- avoid hammering EA on rapid repeat clicks
_cache = {}


class EAApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _get(path, params):
    if "platform" not in params or not params["platform"]:
        raise EAApiError("platform is required", 400)
    if params["platform"] not in PLATFORMS:
        raise EAApiError(f"unknown platform '{params['platform']}'", 400)

    cache_key = (path, tuple(sorted(params.items())))
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(
            BASE_URL + path, params=params, impersonate=IMPERSONATE, timeout=_TIMEOUT
        )
    except requests.exceptions.RequestException as exc:
        raise EAApiError(f"could not reach EA's API: {exc}") from exc

    if resp.status_code == 404:
        raise EAApiError("not found -- check the club ID / name and platform", 404)
    if not resp.ok:
        raise EAApiError(
            f"EA API returned HTTP {resp.status_code}", resp.status_code
        )

    if not resp.text.strip():
        # EA returns an empty 200 body for some "no data" cases (e.g. a club
        # with no matches yet, or an invalid clubId that isn't a hard 404).
        data = None
    else:
        try:
            data = resp.json()
        except ValueError as exc:
            raise EAApiError("EA API returned a non-JSON response") from exc

    _cache[cache_key] = (now, data)
    return data


def _normalize_dict_or_list(data):
    """EA's *Info endpoints return an object keyed by clubId instead of a
    list. Normalize both shapes to a plain list of records."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.values())
    return []


def _search_raw(platform, club_name):
    data = _get("allTimeLeaderboard/search", {"platform": platform, "clubName": club_name})
    return [r for r in _normalize_dict_or_list(data) if r.get("clubId")]


def search_club(platform, club_name):
    parsed = []
    for r in _search_raw(platform, club_name):
        info = r.get("clubInfo") or {}
        parsed.append(
            {
                "clubId": str(r["clubId"]),
                "name": r.get("clubName") or info.get("name", ""),
                "regionId": info.get("regionId"),
                "teamId": info.get("teamId"),
            }
        )
    return parsed


def club_info(platform, club_id):
    data = _get("clubs/info", {"platform": platform, "clubIds": club_id})
    records = _normalize_dict_or_list(data)
    return records[0] if records else None


def overall_stats(platform, club_id):
    data = _get("clubs/overallStats", {"platform": platform, "clubIds": club_id})
    records = _normalize_dict_or_list(data)
    return records[0] if records else None


def division_stats(platform, club_id):
    """currentDivision / bestDivision / points / cleanSheets only come back
    from the search endpoint, not clubs/overallStats -- so look the club back
    up by its own name to get them, regardless of how we arrived at club_id
    (search click, reload, deep link)."""
    info = club_info(platform, club_id)
    if not info or not info.get("name"):
        return None
    for r in _search_raw(platform, info["name"]):
        if str(r.get("clubId")) == str(club_id):
            return r
    return None


def member_career_stats(platform, club_id):
    return _get("members/career/stats", {"platform": platform, "clubId": club_id})


def member_stats(platform, club_id):
    return _get("members/stats", {"platform": platform, "clubId": club_id})


def matches_stats(platform, club_id, match_type="leagueMatch", max_results=10):
    if match_type not in ("leagueMatch", "playoffMatch", "friendlyMatch"):
        raise EAApiError(f"unknown matchType '{match_type}'", 400)
    data = _get(
        "clubs/matches",
        {
            "platform": platform,
            "clubIds": club_id,
            "matchType": match_type,
            "maxResultCount": max_results,
        },
    )
    return data or []
