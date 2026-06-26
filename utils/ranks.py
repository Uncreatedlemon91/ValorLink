"""Helpers for working with the configurable rank ladder in config.RANKS."""
import config


def rank_names() -> list[str]:
    return [r["name"] for r in config.RANKS]


def rank_index(name: str) -> int:
    for i, r in enumerate(config.RANKS):
        if r["name"].lower() == name.lower():
            return i
    raise ValueError(f"Unknown rank: {name}")


def rank_by_name(name: str) -> dict:
    return config.RANKS[rank_index(name)]


def next_rank(name: str) -> str | None:
    i = rank_index(name)
    if i + 1 >= len(config.RANKS):
        return None
    return config.RANKS[i + 1]["name"]


def prev_rank(name: str) -> str | None:
    i = rank_index(name)
    if i - 1 < 0:
        return None
    return config.RANKS[i - 1]["name"]


def display(name: str) -> str:
    """e.g. 'Cpl' for use in nicknames / compact embeds."""
    return rank_by_name(name)["abbreviation"]
