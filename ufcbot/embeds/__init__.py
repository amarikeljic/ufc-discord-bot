"""Discord embeds, message text and graphics.

Layout rules, because most people read these on a phone:

* Keep lines short, one fact per line.
* Join facts with " · " and put non-breaking spaces inside each fact, so a
  narrow screen wraps between facts and never inside one ("Sub (RNC) 12%").
* Tables are fixed-width code blocks. Side-by-side inline fields collapse into
  a single column on mobile and stop reading as a table.

Graphics live in ``embeds.images`` and are imported from there directly, so
Pillow only loads where it is used.
"""

from .cards import card_changes_embed, event_embed, schedule_embed
from .common import UFC_RED, stamp
from .events import (
    scheduled_event_description,
    scheduled_event_location,
    scheduled_event_name,
)
from .fighters import fighter_embed
from .live import (
    live_knockdown_text,
    live_open_embed,
    live_pause_text,
    live_result_embed,
    live_round_embed,
)
from .pickem import (
    pickem_board_embed,
    pickem_card_embed,
    pickem_leaderboard_embed,
    pickem_picker_embed,
    pickem_stats_embed,
)
from .picks import (
    model_status_embed,
    picks_board_embed,
    prediction_embed,
    predictions_embed,
    recap_embed,
    scorecard_embed,
)
from .ratings import rankings_embed, ratings_changes_embed

__all__ = [
    "UFC_RED",
    "card_changes_embed",
    "event_embed",
    "fighter_embed",
    "live_knockdown_text",
    "live_open_embed",
    "live_pause_text",
    "live_result_embed",
    "live_round_embed",
    "model_status_embed",
    "pickem_board_embed",
    "pickem_card_embed",
    "pickem_leaderboard_embed",
    "pickem_picker_embed",
    "pickem_stats_embed",
    "picks_board_embed",
    "prediction_embed",
    "predictions_embed",
    "rankings_embed",
    "ratings_changes_embed",
    "recap_embed",
    "schedule_embed",
    "scheduled_event_description",
    "scheduled_event_location",
    "scheduled_event_name",
    "scorecard_embed",
    "stamp",
]
