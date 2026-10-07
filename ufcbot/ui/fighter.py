"""The buttons under a fighter card.

The card leads with the page ESPN itself leads with -- who this is, the two
records, the rating. The detail sits behind buttons rather than in front of it:
the career numbers, and the fight history, which is the only page that knows
about the fights before the UFC.

The history is fetched the first time somebody asks for it. Most lookups never
open it, and it is a second call to ESPN that would otherwise be made for
everyone to serve a few.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from ..embeds.fighters import fighter_embed, fighter_history_embed, fighter_stats_embed

if TYPE_CHECKING:
    from ..models import Fighter
    from ..stats.rankings import Ranked
    from ..stats.service import FighterCareer

# Just under Discord's fifteen-minute interaction window, the same as the
# pick'em picker: past it the buttons stop working and the card stays put.
CARD_TIMEOUT = 840

OVERVIEW, STATS, HISTORY = "overview", "stats", "history"


class FighterView(discord.ui.View):
    """Three pages of one fighter, with the page being shown marked."""

    def __init__(
        self,
        *,
        profile: Fighter | None,
        career: FighterCareer | None,
        standing: Ranked | None = None,
        pound_for_pound: Ranked | None = None,
        history: Callable[[], Awaitable[list]] | None = None,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(timeout=CARD_TIMEOUT)
        self.profile = profile
        self.career = career
        self.standing = standing
        self.pound_for_pound = pound_for_pound
        self._history = history
        self._loaded: list | None = None
        self.owner_id = owner_id
        self.page = OVERVIEW
        self._draw()

    # -- pages ---------------------------------------------------------------

    def embed(self) -> discord.Embed:
        if self.page == STATS:
            return fighter_stats_embed(self.profile, self.career)
        if self.page == HISTORY:
            return fighter_history_embed(self.profile, self.career, self._loaded or [])
        return fighter_embed(
            self.profile,
            self.career,
            standing=self.standing,
            pound_for_pound=self.pound_for_pound,
        )

    def _draw(self) -> None:
        """One button per page, with the current one greyed out and disabled."""
        self.clear_items()
        for page, label in ((OVERVIEW, "Overview"), (STATS, "Stats"), (HISTORY, "Fight history")):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if page == self.page else discord.ButtonStyle.secondary,
                disabled=page == self.page,
            )
            button.callback = self._turn_to(page)
            self.add_item(button)

    def _turn_to(self, page: str):
        async def go(interaction: discord.Interaction) -> None:
            if self.owner_id is not None and interaction.user.id != self.owner_id:
                # Someone else's card: give them their own copy rather than
                # moving the page under the person who asked for it.
                await interaction.response.send_message(
                    embed=await self._page(page), ephemeral=True
                )
                return
            self.page = page
            embed = await self._page(page)
            self._draw()
            await interaction.response.edit_message(embed=embed, view=self)

        return go

    async def _page(self, page: str) -> discord.Embed:
        if page == HISTORY and self._loaded is None and self._history is not None:
            self._loaded = await self._history()
        if page == STATS:
            return fighter_stats_embed(self.profile, self.career)
        if page == HISTORY:
            return fighter_history_embed(self.profile, self.career, self._loaded or [])
        return fighter_embed(
            self.profile,
            self.career,
            standing=self.standing,
            pound_for_pound=self.pound_for_pound,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
