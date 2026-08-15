"""Tests for the package version details exposed by wrapture."""

import re

import wrapture


def test_version_info_is_a_tuple_of_strings() -> None:
    assert isinstance(wrapture.__version_info__, tuple)
    assert all(isinstance(part, str) for part in wrapture.__version_info__)
    assert len(wrapture.__version_info__) in (3, 4)


def test_version_is_formatted_from_version_info() -> None:
    parts = wrapture.__version_info__
    base = ".".join(parts[:3])

    if len(parts) == 3:
        expected = base
    elif parts[3].startswith(("dev", "post")):
        expected = f"{base}.{parts[3]}"
    else:
        expected = f"{base}{parts[3]}"

    assert wrapture.__version__ == expected


def test_version_is_pep_440_compliant() -> None:
    # Enough of PEP 440 to cover the forms this project uses: a three-part
    # release number with an optional pre-release, dev or post suffix.

    pattern = r"^\d+\.\d+\.\d+((a|b|rc)\d+|\.dev\d+|\.post\d+)?$"
    assert re.match(pattern, wrapture.__version__)
