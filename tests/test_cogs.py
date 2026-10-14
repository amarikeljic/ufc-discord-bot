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


# -- when the database gets copied -------------------------------------------


class Recorder:
    """A bot with just the parts the grading step reaches for."""

    def __init__(self, *, awaiting, graded, backup_dir="/backups"):
        from types import SimpleNamespace

        self.awaiting = list(awaiting)
        self.copies: list[str] = []
        outer = self

        class Storage:
            async def pickem_events_awaiting_grading(self, now):
                return outer.awaiting.pop(0) if outer.awaiting else []

            async def back_up_to(self, directory, **kwargs):
                outer.copies.append(directory)
                return directory

        class Pickem:
            async def grade_due(self):
                return graded

        self.storage = Storage()
        self.pickem = Pickem()
        self.config = SimpleNamespace(backup_dir=backup_dir)


def jobs_for(bot):
    return JobsCog(bot)


async def test_the_database_is_copied_once_a_card_is_finished_with():
    """The one moment it is settled: every bout graded, every point awarded."""
    bot = Recorder(awaiting=[["E1"], []], graded=12)

    await jobs_for(bot)._grade_pickem()

    assert bot.copies == ["/backups"]


async def test_a_card_part_way_through_is_not_a_moment_to_copy():
    """Half the card graded is half a leaderboard."""
    bot = Recorder(awaiting=[["E1"], ["E1"]], graded=4)

    await jobs_for(bot)._grade_pickem()

    assert bot.copies == []


async def test_a_fight_pulled_mid_week_does_not_trigger_a_copy():
    """Fights come off a card all week and their picks are voided as they go.
    Nothing has locked yet, so nothing was awaiting grading, and this is not the
    end of anything."""
    bot = Recorder(awaiting=[[], []], graded=3)

    await jobs_for(bot)._grade_pickem()

    assert bot.copies == []


async def test_nothing_is_written_when_no_destination_is_set():
    bot = Recorder(awaiting=[["E1"], []], graded=12, backup_dir="")

    await jobs_for(bot)._grade_pickem()

    assert bot.copies == []
