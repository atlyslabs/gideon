"""
Typed entity schemas for Graphiti memory extraction.

Passing these into `graphiti.add_episode(entity_types={...})` steers the
LLM extractor toward semantic buckets we actually care about in the UI
(Person, Project, Preference, etc.), and Graphiti writes the corresponding
labels onto the Neo4j `:Entity` nodes so the frontend can color them
without regex heuristics.

All descriptions are read by the LLM at extraction time — keep them
concrete, specific to Gideon's domain, and unambiguous.
"""

from pydantic import BaseModel


# Each class is intentionally fieldless — Graphiti reads the docstring to
# describe the type to the LLM. We just want the matching Neo4j label
# (:Person, :Project, …) attached to the node; extracted attributes live on
# relationships, not as node properties.


class Person(BaseModel):
    """A specific human the user interacts with — teammate, collaborator,
    manager, customer, friend, family. NOT roles like "the user" / "the
    team"; only named or clearly-referenced individuals."""
    pass


class Project(BaseModel):
    """A specific body of work — a feature, migration, launch, initiative,
    or named ongoing effort. NOT ad-hoc tasks, tickets, or meetings."""
    pass


class Company(BaseModel):
    """An organization — employer, vendor, customer company, tool provider,
    partner. NOT products or features of a company."""
    pass


class Preference(BaseModel):
    """A durable statement about what the user likes, dislikes, or always /
    never does. Must be stative ("prefers terse responses") not episodic
    ("said they liked X yesterday")."""
    pass


class Topic(BaseModel):
    """A recurring subject the user talks about — a domain, theme, or
    ongoing concern that isn't a Project. Example: "pricing", "performance",
    "hiring". Use sparingly."""
    pass


# Passed as-is to graphiti.add_episode(entity_types=ENTITY_TYPES).
# Order here doesn't matter — Graphiti picks based on content.
ENTITY_TYPES = {
    "Person": Person,
    "Project": Project,
    "Company": Company,
    "Preference": Preference,
    "Topic": Topic,
}
