"""That the cogs only reach for things that exist.

Commands and background jobs are framework callbacks: nothing in the test suite
calls them and nothing imports them, so a helper that moves house leaves a
`self.something` behind that no linter sees and that only fails at the hour the
job runs. This walks each cog module and checks every attribute it reaches for.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest

from ufcbot.cogs.jobs import JobsCog
from ufcbot.cogs.ufc import UFCCog

COGS = [UFCCog, JobsCog]


def attributes_used(cls) -> set[str]:
    """Every ``self.x`` the module reads, inside this class."""
    source = pathlib.Path(inspect.getfile(cls)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    definition = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__
    )
    return {
        node.attr
        for node in ast.walk(definition)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def attributes_available(cls) -> set[str]:
    """Everything on the class, plus whatever ``__init__`` assigns to self."""
    available = set(dir(cls))
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls.__init__)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        ):
            available.add(node.attr)
    return available


@pytest.mark.parametrize("cls", COGS, ids=lambda c: c.__name__)
def test_a_cog_never_reaches_for_something_it_does_not_have(cls):
    missing = attributes_used(cls) - attributes_available(cls)

    assert not missing, f"{cls.__name__} uses self.{', self.'.join(sorted(missing))}, which is not there"


@pytest.mark.parametrize("cls", COGS, ids=lambda c: c.__name__)
def test_every_loop_is_started_and_cancelled_with_the_cog(cls):
    """A loop that is never started does nothing; one that is never cancelled
    keeps running against a bot that has been torn down."""
    from discord.ext import tasks

    loops = {name for name in dir(cls) if isinstance(getattr(cls, name, None), tasks.Loop)}
    if not loops:
        pytest.skip(f"{cls.__name__} has no background loops")

    started = inspect.getsource(cls.cog_load)
    cancelled = inspect.getsource(cls.cog_unload)

    for loop in loops:
        assert f"self.{loop}.start()" in started, f"{loop} is never started"
        assert f"self.{loop}.cancel()" in cancelled, f"{loop} is never cancelled"
