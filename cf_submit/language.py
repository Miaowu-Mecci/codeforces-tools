import re

from bs4 import Tag

from .exceptions import LanguageNotFound

LANGUAGE = "GNU G++23"
_GNU_CPP23 = re.compile(r"GNU G\+\+23(?:\s+[0-9][0-9.]*(?:\s+\([^()]+\))?)?", re.I)
_GCC_CPP23 = re.compile(r"C\+\+23\s*\(GCC(?:\s+[^()]+)?\)", re.I)


def is_target_language(label: str) -> bool:
    normalized = " ".join(label.split())
    return bool(_GNU_CPP23.fullmatch(normalized) or _GCC_CPP23.fullmatch(normalized))


def enabled_option(option: Tag) -> bool:
    return not (option.has_attr("disabled") or option.find_parent("optgroup", disabled=True))


def choose_language(select: Tag) -> tuple[str, str]:
    options = {
        option["value"]: option.get_text(" ", strip=True)
        for option in select.find_all("option")
        if enabled_option(option)
        and re.fullmatch(r"[1-9][0-9]*", option.get("value", ""))
        and is_target_language(option.get_text(" ", strip=True))
    }
    if not options:
        raise LanguageNotFound("GNU G++23 is unavailable in this submission form.")
    if len(options) != 1:
        raise LanguageNotFound("Multiple GNU G++23 options are available. Cannot select one safely.")
    return next(iter(options.items()))
