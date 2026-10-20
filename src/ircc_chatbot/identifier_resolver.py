"""Turn an identifier as the user typed it into the form stored in the database.

Stored identifiers are <PREFIX><number> with no separator and no leading zeros, e.g. SREQ123.
All of these resolve to SREQ123 for stereotype SREQ:
    "123", "SREQ123", "SREQ 123", "SREQ-123", "sreq_123", "SREQ is 123", "SREQ#123"

What: one function, resolve_identifier, used by intent.py for lookups.

How: normalise case and spacing, split into letters + digits with one regex,
check the letters belong to this stereotype, drop leading zeros, and rebuild the
identifier from the prefix in metadata (identifierPrefix / identifierAliases).

Why: the SQL does an exact match (WHERE [Name] = @p1), so one wrong character
returns no rows. This is done in code, not by the LLM, because it must be exact
every time and testable. The LLM is told to copy the identifier as typed.
Anything that doesn't match is rejected, never guessed.
"""

from __future__ import annotations

import re

from .errors import IdentifierError
from .metadata_loader import Metadata, Stereotype

MAX_DIGITS = 9

# Optional letters, optional separators (space - _ . : #), optional word "is", then digits.
_PATTERN = re.compile(r"^([A-Z]*)[ \-_.:#]*(?:IS )?([0-9]+)$")


def resolve_identifier(raw: str, stereotype: Stereotype, metadata: Metadata) -> str:
    """Return the stored form of `raw` for `stereotype`, e.g. "SREQ-0123" -> "SREQ123".

    Steps:
      1. Reject empty input; uppercase and collapse whitespace ("  Sreq  124 " -> "SREQ 124").
      2. Split into optional letters and the digits with _PATTERN.
      3. Reject more than MAX_DIGITS digits (phone numbers, typos).
      4. If letters were typed, they must be this stereotype's prefix or alias. If they
         belong to another stereotype, say which one (clearer than "invalid").
      5. Drop leading zeros, reject 0, and prepend the prefix from metadata.

    Raises IdentifierError with a message that shows the expected form.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise IdentifierError(f"No identifier was given for {stereotype.code}.")

    text = " ".join(raw.strip().upper().split())
    match = _PATTERN.match(text)
    example = f"{stereotype.identifier_prefix}123"
    if not match:
        raise IdentifierError(
            f"'{raw}' is not a valid {stereotype.code} identifier. Expected a form like {example}."
        )

    letters, digits = match.groups()
    if len(digits) > MAX_DIGITS:
        raise IdentifierError(f"'{raw}' has too many digits to be an identifier.")

    if letters and letters != stereotype.identifier_prefix and letters not in stereotype.identifier_aliases:
        owner = _owner_of(letters, metadata)
        if owner:
            raise IdentifierError(
                f"'{raw}' is a {owner} identifier, but the question is about {stereotype.code} "
                f"({stereotype.display_name})."
            )
        raise IdentifierError(
            f"'{raw}' is not a valid {stereotype.code} identifier. Expected a form like {example}."
        )

    # Stored numbers never have leading zeros: "USTP-001" is stored as USTP1.
    number = digits.lstrip("0")
    if not number:
        raise IdentifierError(f"'{raw}' is not a valid {stereotype.code} identifier. The number can't be 0.")
    return f"{stereotype.identifier_prefix}{number}"


def _owner_of(letters: str, metadata: Metadata) -> str | None:
    """Return the stereotype code whose prefix or alias is `letters`, or None.

    Used only to give a better error: "'UC-45' is a UC identifier, but the question is about SREQ".
    """
    for code, st in metadata.stereotypes.items():
        if letters == st.identifier_prefix or letters in st.identifier_aliases:
            return code
    return None
