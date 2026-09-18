# UFC Discord Bot

A Discord bot for UFC fans. It follows the UFC calendar, shows fight cards and exact
ufcstats.com fighter stats, predicts every fight with a machine learning model, posts
live round-by-round coverage during cards, and keeps a public record of how its picks
have done.

## Features

- **Fight cards and schedule.** Upcoming cards, full fight cards, results and venues.
- **Fighter stats.** The same career numbers ufcstats.com publishes, such as strikes
  landed per minute, striking and takedown accuracy and defence, and submission average.
  Also shows record, reach, stance and headshot.
- **Predictions.** Win probability for any matchup, plus how the fight is likely to end:
  KO/TKO, submission, unanimous or split decision, with the likeliest finishing technique.
- **Picks board.** One auto-updating message per upcoming card with every pick, method
  and DraftKings moneyline. Picks lock when the card starts and are graded afterwards.
- **Scorecard.** A running record of the model's accuracy, split by confidence, with the
  betting favourites' record over the same fights as a benchmark.
- **Live coverage.** During a card: a preview as fighters walk out, with both fighters'
  tale of the tape and career stats, then knockdown alerts, a note when the referee
  stops the action mid-round, stats after every round, and the official result with
  judges' scores. Previews and results include a side-by-side headshot graphic.
- **Card changes.** A fight coming off the card, a short-notice replacement or a bout
  added late is announced as soon as it shows up, days before the card if that is when
  it happens.
- **Pick'em.** Members pick winners for the next UFC card from a private menu once odds
  are posted. A right pick scores what a 100-point bet would win at those odds, so
  underdogs pay more; a wrong pick costs 100 points. The channel holds just the current
  card and a server leaderboard, and members can check their win rate and card-by-card
  history.
- **Discord scheduled events.** Every upcoming card mirrored into your server's events.

## Commands

| Command | What it does |
| --- | --- |
| `/ufc results [event]` | Results from the latest or a named card |
| `/ufc fighter <name>` | Fighter profile and career stats |
| `/ufc predict <a> <b> [rounds] [title]` | Head-to-head prediction |
| `/ufc predictions [event]` | Picks for every fight on a card |
| `/ufc scorecard` | The model's record since tracking began |
| `/ufc pickem stats [member]` | Points, rank, win rate and card history |
| `/ufc pickem card <event> [member]` | Pick-by-pick results for one card |
| `/ufc channels set` | Choose channels for picks, accuracy, schedule and live coverage |
| `/ufc channels status` / `refresh` / `clear` | Manage those channels |
| `/ufc sync enable` / `disable` / `now` / `status` / `settings` | Discord scheduled events |
| `/ufc model status` | Dataset freshness and model accuracy |
| `/ufc model refresh` | Download new data and retrain (bot owner only) |

The `channels` commands need **Manage Server**, and the `sync` commands need **Manage Events**.

Upcoming cards, the schedule and the pick'em board and leaderboard are not commands: they
are the auto-updating boards in the channels set by `/ufc channels set`. Picks are made
with the buttons on the pick'em board.

## Setup

Requires Python 3.11 or newer.

**1. Create a Discord application.** At <https://discord.com/developers/applications>,
create an application, open the **Bot** tab and copy the token. No privileged intents
are needed.

**2. Invite the bot** with the `bot` and `applications.commands` scopes and the
permissions Manage Events, View Channel, Send Messages, Embed Links and Attach Files:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=8589986816&scope=bot%20applications.commands
```

**3. Install.**

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put your token in `.env`. While testing, set `DEV_GUILD_IDS` to your server's ID so
commands appear immediately instead of within an hour.

**4. Train the model (optional).**

```bash
python train.py
```

This downloads the fight dataset (about 10 MB) and trains the model in a minute or two.
If you skip it, the bot does the same shortly after starting, in a process of its own.

**5. Run.**

```bash
python bot.py
```

**6. Set up channels** in your server, for example:

```
/ufc channels set predictions:#fight-picks accuracy:#scorecard schedule:#fight-calendar live:#fight-night pickem:#pickem
/ufc sync enable
```

## Configuration

All settings live in `.env`; see `.env.example` for descriptions.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Bot token |
| `DEV_GUILD_IDS` | empty | Servers where commands register instantly |
| `DATABASE_PATH` | `ufcbot.sqlite3` | SQLite database file |
| `SYNC_INTERVAL_MINUTES` | `180` | How often scheduled events sync |
| `DEFAULT_DAYS_AHEAD` | `60` | How far ahead to track cards |
| `DEFAULT_EVENT_DURATION_MINUTES` | `240` | Length of each scheduled event |
| `DEFAULT_START_ANCHOR` | `main_card` | Scheduled events start at `main_card` or `prelims` |
| `ENABLE_POSTER_ART` | `true` | Attach poster art to scheduled events |
| `CACHE_TTL_SECONDS` | `900` | How long API responses are cached |
| `ENABLE_PREDICTIONS` | `true` | Stats, predictions and picks |
| `DATA_DIR` / `MODEL_DIR` | `data` / `models` | Dataset and trained model locations |
| `STATS_REFRESH_HOURS` | `24` | How often to check for new fight data |

## How it works

### Update schedule

| What | How often |
| --- | --- |
| Live channel | Every 15 seconds during a card |
| Picks, schedule and scorecard boards | Every 20 minutes |
| Changes to upcoming cards | Every 20 minutes |
| Odds and fight card data | Cached for 10 and 15 minutes |
| Discord scheduled events | Every 3 hours |
| Fight dataset and model | Checked daily; every 6 hours after a card until new data arrives |

Boards are edited in place, and only when their content changes, so an unchanged refresh
makes no Discord API calls.

A change to a card — a withdrawal, a replacement, a bout added — reaches the boards
within about half an hour: up to fifteen minutes of cached card data, then the next
twenty-minute pass. What the boards cannot do is get ahead of ESPN, which is often a day
or more behind on short-notice changes. When a fight's two fighters are no longer the two
the pick was made for, the pick is dropped rather than shown against a fight that is not
happening.

### Pick'em scoring

A right pick scores what a 100-point bet would win at the odds. A wrong pick costs 100
points, favourite or underdog.

| Odds when you pick | Right | Wrong |
| --- | --- | --- |
| -290 favourite | +34 | -100 |
| -110 | +91 | -100 |
| +110 underdog | +110 | -100 |
| +235 underdog | +235 | -100 |

Points are locked in with the odds at the moment you pick; changing a pick uses the
new odds. The game runs on the next UFC card only. Picks open once a sportsbook posts
lines for a fight and lock when that part of the card starts. Draws, no contests and
cancelled fights are void, and so is a pick on a fighter who was replaced before the
bell: that fight never happened, so the pick scores nothing either way rather than
counting as a loss. A fight coming off the card is voided as soon as the bot sees it
go, rather than sitting in your picks as pending until the card is over.
Other members' picks stay hidden until the fight locks,
though the board shows the overall split. When the card is over and every pick is
scored, its board is deleted and the next card's board replaces it once odds are up.

### Where the odds come from

ESPN carries DraftKings moneylines for numbered UFC cards, but nothing for Dana White's
Contender Series. Any fight ESPN leaves unpriced falls back to Polymarket, a prediction
market whose price is a probability: 0.545 means a 54.5% favourite, which reads as -120.
Because the two sides sum to 1, that price carries no bookmaker margin.

Wherever odds appear the source is named, so "Odds (Polymarket)" on a walkout preview and
"Odds from DraftKings" on the picks and pick'em boards. A card drawing on both sources
lists both.

### The prediction model

Every decided UFC fight becomes a training example, built from both fighters' records
**as they stood before that fight**, so the model never sees information from the future.
Inputs include age, height, reach and stance; record, streaks and layoff; the ufcstats.com
career rates; knockdowns, control time and finish rates; and the differences between the
two fighters.

Two models work together. A blend of gradient boosting and logistic regression predicts
the winner. A second model, trained on how winners won, splits that probability into
KO/TKO, submission, unanimous decision and split decision. The likeliest finishing
technique comes from the winner's finishing history, how the opponent has been finished
before, and the league-wide rate.

Accuracy on the most recent 18 months of fights, held out from training:

| Prediction | Model | Simple baseline |
| --- | --- | --- |
| Winner | 64.7% | 59.2% (better record wins) |
| Method, when the winner is known | 50.8% | 39.8% (always unanimous decision) |
| Winner and method together | 34.5% | |
| Technique, when the finish type is known | 73.9% | 73.4% (most common technique) |

Win probabilities are calibrated: fights scored at 65% are won about 65% of the time.
Technique predictions barely beat the base rate, because most knockouts are punches and
most submissions are rear-naked chokes. Neither data source separates KO from TKO.

### Keeping data current

Fight statistics come from [Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats),
which republishes ufcstats.com data the morning after each card. The bot checks for
changes using ETags, validates each download in a staging folder before using it, then
rebuilds career stats and retrains.

That whole job runs in a process of its own, started when it is needed and gone when it
finishes. It leaves two files behind, and the bot picks them up and swaps them in:

| File | What it holds |
| --- | --- |
| `data/career.pkl` | Every fighter's career totals and tale of the tape |
| `models/ufc_model.pkl` | The trained model as plain numbers: tree nodes and coefficients |

### Memory

The bot holds about 60 MB. Parsing the dataset needs pandas and training needs
scikit-learn, which between them cost around 150 MB resident and another 100 MB while
training runs — for a job that runs once a day. So they stay in the refresh process,
which peaks near 350 MB for a minute or two and then exits, giving all of it back.

Scoring a fight does not need either library: training writes the fitted trees and
coefficients out as plain numbers, and `scorer.py` walks them with nothing but the
standard library. It is the same arithmetic, so the compiled model returns what the
fitted one returned — training checks that on real fights every time and refuses to save
a model that disagrees by more than a rounding error.

One consequence is that a machine that only runs the bot does not need pandas or
scikit-learn installed at all, as long as `data/` and `models/` are filled in by
`python train.py` somewhere else.

### Live coverage

ESPN's play-by-play marks round starts and ends, knockdowns, pauses in the action and
results. ESPN only publishes running stat totals, so each round's numbers are the
difference between the totals at consecutive round ends. Each update is recorded in the
database, so a restart mid-card never repeats a post.

A pause is the clock stopping mid-round — a foul, a doctor's look, a lost mouthpiece.
ESPN records that it happened but never why, so the post says only that, and the restart
is left unsaid: one message per stoppage.

Fights are fought one at a time, so the poll only asks about the fight under way, the
next couple, and any that have finished without their result being posted. On a
twelve-fight card that is about three requests every fifteen seconds instead of twelve.

### Card changes

Withdrawals and replacements are not in the play-by-play, which only covers a card while
it is being fought. Instead the fights on each upcoming card are remembered and compared
with the next reading, every twenty minutes, so a change is found whenever it happens.
Changes are posted to the live channel, or the schedule channel when there is no live
channel set.

Anything that would read as a fight being cancelled is only believed when the card read
cleanly. A response missing a fight, or missing the fighters within one, is left alone
until the next pass — the same rule that decides when a pick is dropped or a pick'em pick
is voided.

The walkout preview pairs ESPN's bio (record, age, height, weight, reach, stance and
country) with the ufcstats.com career numbers for both fighters. A fighter who is not in
the dataset yet, such as a debutant, still gets the ESPN rows.

Every live post names its card: the embeds carry it in the footer alongside the time they
were built, and the knockdown and pause alerts, which are plain messages with no footer
to put it in, carry it on the end of the line. Channel boards label their footer "Last
updated" instead, because the bot edits those messages in place.

## Data sources

- **ESPN's public MMA API:** calendar, fight cards, fighter profiles, DraftKings odds,
  results, play-by-play, fight stats and judges' scores.
- **[Polymarket](https://gamma-api.polymarket.com):** odds for fights ESPN does not price,
  Contender Series above all. No account or API key is needed.
- **[Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats):**
  round-by-round fight statistics and fighter measurements from ufcstats.com.
- **TheSportsDB:** optional event poster art.

These are unofficial sources whose formats can change. Failures are logged and the bot
keeps running without the affected feature.

## Project structure

```
bot.py                  Start the bot
train.py                Download fight data and train the model
ufcbot/
  bot.py                Bot setup and shared services
  config.py             Settings from .env
  instance.py           Prevents two copies running at once
  models.py             Event, Bout and Fighter
  storage.py            SQLite database
  util.py               Small helpers
  cogs/
    ufc.py              Slash commands and background jobs
  sources/
    http.py             HTTP client with caching and retries
    espn.py             ESPN schedules, cards, fighters, odds and live data
    posters.py          Poster art from TheSportsDB
  features/
    sync.py             Discord scheduled events
    tracking.py         Records picks before a card and grades them after
    channels.py         Picks, schedule and scorecard boards
    live.py             Live fight coverage
    cardwatch.py        Spots and announces changes to upcoming cards
    pickem.py           Pick'em rules: scoring, locks, saving and grading picks
  ui/
    pickem.py           Pick'em buttons and the private picker
  embeds/
    common.py           Colours and text helpers
    cards.py            Fight card, schedule and fighter embeds
    picks.py            Picks, prediction, recap and scorecard embeds
    live.py             Live coverage embeds
    pickem.py           Pick'em board, picker, leaderboard and stats
    images.py           Headshot matchup graphics
  stats/
    service.py          Stats and predictions, and running the refresh job
    worker.py           The refresh job: download, validate, rebuild, retrain
    dataset.py          Download and parse the ufcstats.com dataset
    career.py           Career totals and the ufcstats.com formulas
    techniques.py       Finish methods and techniques
    names.py            Match fighter names across sources
    features.py         Model inputs
    model.py            Training the winner and method models
    scorer.py           The trained model, compiled to run without scikit-learn
    prediction.py       A prediction and how it is put together
```

`dataset.py` and `model.py` are the only modules that use pandas and
scikit-learn, and only `worker.py` imports them. See
[Memory](#memory) for why that matters.

## Troubleshooting

**Commands don't appear.** Global commands can take up to an hour. Set `DEV_GUILD_IDS`
for instant registration, and check the bot was invited with `applications.commands`.

**"Another copy of the bot is already running."** Only one copy can use a database at a
time. Stop the other one first.

**"The model is still downloading data and training."** First start takes a minute or
two. Run `python train.py` beforehand to skip the wait.

**A fighter has no pick or stats.** Fighters appear once they have a UFC fight in the
dataset, so debuts and Contender Series bouts have no pick. The same goes for a
short-notice replacement: the fight stays on the card and loses its pick until the new
fighter has UFC history.

**A card still shows a fight that was changed.** Fight cards come from ESPN, which can be
a day or more behind on withdrawals and replacements. The boards follow within about half
an hour of ESPN updating; `/ufc channels refresh` only helps once it has.

**Upgrading from a version with `models/ufc_predictor.joblib`.** That file is no longer
read and can be deleted. The first refresh writes `models/ufc_model.pkl` and
`data/career.pkl` in its place, which takes a minute or two.
