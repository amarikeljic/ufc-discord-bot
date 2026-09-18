"""Pick'em buttons and the private picker.

The two buttons on the card's board are persistent: their custom ids carry the
event id, and ``PICKEM_BUTTONS`` is registered with the bot at startup, so they
keep working after a restart. The picker itself is an ephemeral message with one
winner menu per fight, four fights to a page.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import discord

from ..embeds.pickem import pickem_card_embed, pickem_picker_embed
from ..features.pickem import NO_ODDS, OPEN, bout_status, points_for
from ..util import format_odds, truncate

if TYPE_CHECKING:
    from ..features.pickem import PickemService
    from ..models import Bout, Event
    from ..storage import PickemRecord

PAGE_SIZE = 4
PICKER_TIMEOUT = 840  # just under Discord's 15-minute interaction window


async def open_picker(interaction: discord.Interaction) -> None:
    """Show the private picker for the card pick'em is running on."""
    if interaction.guild_id is None:
        await interaction.response.send_message("Pick'em works inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    service: PickemService = interaction.client.pickem

    event = await service.current_event()
    if event is None:
        await interaction.followup.send(
            "No card is open for picks right now. Picks open once odds are posted.", ephemeral=True
        )
        return

    view = PickerView(service, event, user_id=interaction.user.id, guild_id=interaction.guild_id)
    embed = await view.refresh()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PickerView(discord.ui.View):
    def __init__(self, service: PickemService, event: Event, *, user_id: int, guild_id: int, page: int = 0) -> None:
        super().__init__(timeout=PICKER_TIMEOUT)
        self.service = service
        self.user_id = user_id
        self.guild_id = guild_id
        self.page = page
        self.notice: str | None = None
        self.set_event(event)

    def set_event(self, event: Event) -> None:
        self.event = event
        self.bouts = event.fights
        self.pages = max(1, math.ceil(len(self.bouts) / PAGE_SIZE))
        self.page = min(self.page, self.pages - 1)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def refresh(self) -> discord.Embed:
        """Rebuild the menus from saved picks and return the matching embed."""
        picks = {
            pick.bout_id: pick
            for pick in await self.service.storage.pickem_user_picks(self.guild_id, self.user_id, self.event.id)
        }
        now = self.service.now()
        self.clear_items()
        for bout in self.bouts[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]:
            self.add_item(BoutSelect(self, bout, picks.get(bout.id), now))

        if self.pages > 1:
            back = discord.ui.Button(label="◀ Back", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=4)
            back.callback = self._back
            forward = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.pages - 1, row=4
            )
            forward.callback = self._forward
            self.add_item(back)
            self.add_item(forward)

        return pickem_picker_embed(self.event, picks, page=self.page, pages=self.pages, now=now, notice=self.notice)

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        self.page = max(0, min(self.pages - 1, self.page + step))
        self.notice = None
        embed = await self.refresh()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _forward(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)


class BoutSelect(discord.ui.Select):
    """A two-option winner menu for one fight."""

    def __init__(self, picker: PickerView, bout: Bout, current: PickemRecord | None, now) -> None:
        self.picker = picker
        self.bout = bout
        status = bout_status(picker.event, bout, now)

        options = []
        for fighter in bout.fighters[:2]:
            odds = bout.odds.get(fighter.id)
            description = f"{format_odds(odds)} · +{points_for(odds)} pts if right" if odds is not None else "No odds yet"
            options.append(
                discord.SelectOption(
                    label=truncate(fighter.display_name, 100),
                    value=fighter.id,
                    description=description,
                    default=current is not None and current.athlete_id == fighter.id,
                )
            )

        if status == OPEN:
            suffix = "pick a winner"
        elif status == NO_ODDS:
            suffix = "odds not posted"
        else:
            suffix = "locked"
        super().__init__(
            placeholder=truncate(f"{bout.matchup}: {suffix}", 150),
            options=options,
            min_values=1,
            max_values=1,
            disabled=status != OPEN,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        picker = self.picker
        outcome = await picker.service.make_pick(
            picker.guild_id, picker.user_id, picker.event.id, self.bout.id, self.values[0]
        )
        fresh = await picker.service.load_event(picker.event.id)
        if fresh is not None:
            picker.set_event(fresh)  # show the latest odds
        picker.notice = ("✅ " if outcome.ok else "⚠️ ") + outcome.message
        embed = await picker.refresh()
        await interaction.edit_original_response(embed=embed, view=picker)


# -- persistent buttons on the card's board ------------------------------------------------


class OpenPickerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pickem:open:(?P<event_id>[0-9]+)"):
    def __init__(self, event_id: str, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Make your picks",
                emoji="🎯",
                style=discord.ButtonStyle.success,
                custom_id=f"pickem:open:{event_id}",
                disabled=disabled,
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["event_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_picker(interaction)


class MyPicksButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pickem:mine:(?P<event_id>[0-9]+)"):
    def __init__(self, event_id: str) -> None:
        super().__init__(
            discord.ui.Button(label="My picks", style=discord.ButtonStyle.secondary, custom_id=f"pickem:mine:{event_id}")
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["event_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        storage = interaction.client.storage
        picks = await storage.pickem_user_picks(interaction.guild_id, interaction.user.id, self.event_id)
        event_name = picks[0].event_name if picks else "this card"
        await interaction.response.send_message(
            embed=pickem_card_embed(interaction.user, event_name, picks), ephemeral=True
        )


PICKEM_BUTTONS = (OpenPickerButton, MyPicksButton)


def board_view(event_id: str, *, accepting: bool) -> discord.ui.View:
    """The two buttons under the card's pick'em board."""
    view = discord.ui.View(timeout=None)
    view.add_item(OpenPickerButton(event_id, disabled=not accepting))
    view.add_item(MyPicksButton(event_id))
    return view
