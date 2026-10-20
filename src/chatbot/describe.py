"""Answer describeStereotype intents ("what fields does SREQ have?") from metadata.

What: turns a DescribeStereotypeIntent into a list of attribute names and
descriptions. No SQL is built and the database is not touched.

How: reads the stereotype's attributes straight from the loaded metadata.

Why: analysts often don't know which fields exist before asking for them. Only
business names and descriptions are returned; physical column names stay inside
the metadata and are never shown to the user.
"""

from __future__ import annotations

from dataclasses import dataclass

from .intent import DescribeStereotypeIntent


@dataclass(frozen=True)
class AttributeInfo:
    """One attribute as shown to the user: business name (e.g. TAG) and its description."""
    name: str
    description: str


@dataclass(frozen=True)
class DescribeResult:
    """The answer to a describeStereotype question.

    stereotype is the code (SREQ), display_name the readable name (System
    Requirement), attributes the fields in metadata order. Frozen, like every
    result object, so it can't be changed after it is built.
    """
    stereotype: str
    display_name: str
    attributes: tuple[AttributeInfo, ...]


def describe_stereotype(intent: DescribeStereotypeIntent) -> DescribeResult:
    """Build the attribute list for one stereotype.

    Copies only name and description from each Attribute, so column names and data
    types never leave the metadata.
    """
    st = intent.stereotype
    return DescribeResult(
        stereotype=st.code,
        display_name=st.display_name,
        attributes=tuple(AttributeInfo(a.name, a.description) for a in st.attributes),
    )
