# UFC Discord Bot

A Discord bot for UFC fans. It follows the UFC calendar, shows fight cards and exact
ufcstats.com fighter stats, predicts every fight with a machine learning model, posts
live round-by-round coverage during cards, and keeps a public record of how its picks
have done.

## Features

- **Fight cards and schedule.** Upcoming cards, full fight cards, results and venues.
- **Fighter stats.** The same career numbers ufcstats.com publishes, such as strikes
  landed per minute, striking and takedown accuracy and defence, and submission average.
  Also shows record, reach, stance, headshot, and the fighter's rating and where it
  places them in their division.
- **Predictions.** Win probability for any matchup, plus how the fight is likely to end:
  KO/TKO, submission, unanimous or split decision, with the likeliest finishing technique.
- **Picks board.** One auto-updating message per upcoming card with every pick and the
  method it is likely to come by. Picks lock when the card starts and are graded
  afterwards. No betting lines: those belong in pick'em.
- **Scorecard.** A running record of the model's accuracy, split by confidence, with the
  betting favourites' record over the same fights as a benchmark.
- **Live coverage.** During a card: a preview as fighters walk out, with both fighters'
  tale of the tape and career stats, then stats after every round, and the official
  result with judges' scores. Previews and results include a side-by-side headshot
  graphic. A result redraws the picks and pick'em boards at once, so nothing on screen
  disagrees with the live channel.
- **Card changes.** A fight coming off the card, a short-notice replacement or a bout
  added late is announced as soon as it shows up, days before the card if that is when
  it happens. Replacements are also written down with how much warning the fighter had:
  nothing reads that yet, but no public dataset records it and the bot is already
  watching, so it is a model input next season that cannot be bought today.
- **Ratings moves.** When a division's board changes, the move is announced with the
  reason: in, out, up or down, after a win, a loss, a long layoff, or results around them.
- **Pick'em.** Members pick winners for the next UFC card from a private menu once odds
  are posted. A right pick scores what a 100-point bet would win at those odds, so
  underdogs pay more; a wrong pick costs 100 points. The channel keeps an all-time
  leaderboard and one for the card being fought, and both show what the model and the
  favourites scored on the same fights, so you can see whether you beat them.
- **Ratings.** A board per division and a pound-for-pound board, ranked by the rating the
  model itself trains on: everyone starts level and a win moves it by how good the fighter
  beaten was. A rating fades once a fighter has been out over a year, and fighters too
  close to separate share a rank.
- **Discord scheduled events.** Every upcoming card mirrored into your server's events,
  and ended as soon as the card's last result is in rather than hours later. A card with
  nobody named yet is left off until a fighter is announced.

## Commands

| Command | What it does |
| --- | --- |
| `/ufc results [event]` | Results from the latest or a named card |
| `/ufc fighter <name>` | Fighter profile and career stats |
| `/ufc predict <a> <b> [rounds] [title]` | Head-to-head prediction |
| `/ufc predictions [event]` | Picks for every fight on a card |
| `/ufc pickem stats [member]` | Points, rank, win rate and card history |
| `/ufc pickem picks <event>` | Everyone's picks for one card, fight by fight |
| `/ufc server` | Latency, uptime, memory and how current the data is |
| `/ufc channels set` | Choose channels for picks, accuracy, schedule, live coverage, pick'em and ratings |
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
| `DEFAULT_DAYS_AHEAD` | `60` | How far ahead to track cards |
| `DEFAULT_EVENT_DURATION_MINUTES` | `240` | Length of each scheduled event |
| `DEFAULT_START_ANCHOR` | `main_card` | Scheduled events start at `main_card` or `prelims` |
| `ENABLE_POSTER_ART` | `true` | Attach poster art to scheduled events |
| `CACHE_TTL_SECONDS` | `900` | How long API responses are cached |
| `ENABLE_PREDICTIONS` | `true` | Stats, predictions and picks |
| `DATA_DIR` / `MODEL_DIR` | `data` / `models` | Dataset and trained model locations |
| `STATS_REFRESH_HOURS` | `24` | How often to check for new fight data |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## How it works

### Update schedule

| What | How often |
| --- | --- |
| Live channel | Every 15 seconds during a card |
| Picks, schedule and scorecard boards | On the hour, and the moment a live post goes out |
| Changes to upcoming cards | On the hour |
| Moves on the ratings boards | On the hour |
| Fight dataset and model | On the hour, and acted on only when it is due |
| Odds and fight card data | Cached for 10 and 15 minutes |
| Discord scheduled events | On the midnight Central pass; ended on the card's last result |

The two loops live in `cogs/jobs.py`, apart from the slash commands, because the two
answer to different things: a command answers a person and returns, a job answers a clock
and has to survive whatever it finds. Anything that happens once a day is a step of the
hourly pass rather than a loop of its own — mirroring the calendar runs on the one pass a
day that lands at midnight Central.

Everything but live coverage and the nightly sync happens in one pass on the hour, in the
order the steps depend on each other: check for new fight data, grade what has finished,
then announce what changed and redraw the boards. New ratings are therefore announced and
drawn by the same pass that downloaded them. The pass is on the hour rather than on a
timer from start-up, so "last updated" means the same thing whether the bot was restarted
at noon or at half past, and it also runs once immediately on start-up so a restart does
not leave stale boards up until the hour.

Checking for fight data is not the same as downloading it. `STATS_REFRESH_HOURS` (24 by
default) is how long the bot waits between real checks; while it knows a card is missing
upstream it checks every hour instead, and an unchanged check is four conditional
requests that come back "not modified".

Boards are edited in place, and only when their content changes, so an unchanged refresh
makes no Discord API calls.

A change to a card — a withdrawal, a replacement, a bout added — reaches the boards
within about an hour and a quarter: up to fifteen minutes of cached card data, then the
next pass on the hour. What the boards cannot do is get ahead of ESPN, which is often a
day or more behind on short-notice changes. When a fight's two fighters are no longer the two
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
new odds. The game runs on the next UFC card only, and opens 48 hours before the first
bell once every fight on that card has been priced — or, if some fight never gets a line,
on whatever prices exist by then, so one prelim cannot keep the whole server from playing.
Each pick locks when that part of the card starts. Draws, no contests and
cancelled fights are void, and so is a pick on a fighter who was replaced before the
bell: that fight never happened, so the pick scores nothing either way rather than
counting as a loss. A fight coming off the card is voided as soon as the bot sees it
go, rather than sitting in your picks as pending until the card is over.
Other members' picks stay hidden until the fight locks,
though the board shows the overall split. `/ufc pickem picks <event>` lays out a whole
card fight by fight afterwards: who backed whom, at what price, and what it scored them.
The channel holds two messages. **All Time Pick'em Leaderboard** is never deleted, only
edited. Below it sits one message that turns over with the card, and it is only ever in
one of three states:

| State | When | Buttons |
| --- | --- | --- |
| **Last Card's Pick'em Leaderboard** | between cards | My picks |
| The card's pick'em board | from 48 hours before the first bell | Make your picks, My picks |
| **This Card's Pick'em Leaderboard** | from the first bell until the last result | My picks |

Moving between the three deletes that message and posts a new one, rather than editing it
in place. A card opening for picks, or a card ending, should read as something happening in
the channel; a message quietly changing under everyone reads as nothing at all. It also
puts the new state at the bottom, under the all-time board.

The third state ends on the last result rather than on the clock. When live coverage posts
a result it checks whether every fight on the card now has one, and if it does, the
leaderboard goes back to **Last Card's** and the Discord event for that card is ended
there and then — within about a minute of the last fight, instead of running on to the end
time it was given days earlier.

Both leaderboards carry two extra lines under the standings: what the model scored on the
same fights, and what backing every favourite would have scored. Neither is ranked among
the players. They pick every fight where a member picks the ones they like, and neither is
playing for anything, so ranking them would be scoring two different games together. They
are there to answer the only question a leaderboard cannot: not who is top, but whether
anyone is actually beating the bot. Only fights that were graded, ended with a winner and
had both prices count towards them, which is exactly the set a member could have played.

### Where the odds come from

Odds appear in pick'em, where they are what you are playing for, and on live coverage,
where they are the closing price on a fight about to happen. The picks board carries none:
it is the model's opinion, and a price next to it only invites the two to be confused.

All of them come from [Polymarket](https://gamma-api.polymarket.com). ESPN carries a
sportsbook line too, but only DraftKings and only patchily — checked across 63 bouts on ten
cards it was the only book ESPN offered at all, and it priced one fight of thirteen on a
numbered card and none at all on Contender Series, where the market priced every one. UFC's
official betting partner is bet365, which publishes no public API and does not appear
anywhere in ESPN's odds feed, so it is not an option.

A prediction market price is a probability rather than a bookmaker's line: 0.545 means a
54.5% favourite, which reads as -120. Because the two sides sum to 1 it carries no
bookmaker margin, which makes it a fairer thing to stake pick'em points on than a price
built to take a cut.

Every pick is still stored with the price that stood when it was made, which is what lets
the scorecard and the pick'em leaderboards measure the model and the members against the
fighters the market favoured.

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

Ratings carry strength of schedule. Every fighter starts at 1000 and each result moves
both fighters by how surprising it was, so beating a contender is worth more than beating
a debutant, and a record built against nobody reads differently from the same record built
against contenders. The model sees each fighter's rating, the average rating of everyone
they have faced, the average of those they beat and those they lost to, and their best
win. It also sees which division the fight is at: heavyweights finish each other far more
often than flyweights and their fights turn on one punch.

A second rating is fitted only on how fights ended, scoring a stoppage as a win, being
stopped as a loss and a decision as half each, so the method model can see finishing power
against the durability it met.

It is given the matchup as well as the two fighters. Every other input measures a fighter
alone, or the gap between them, which says who is the better wrestler but never whether
this wrestler can take this opponent down. So striking volume is set against the
opponent's striking defence, takedowns against takedown defence, knockdown rate against
how often the opponent has been stopped, and so on, in both directions.

The boosting and regularisation settings were searched over 108 combinations by
time-series cross-validation inside the training period, never touching the holdout.
Nothing beat what is here by more than rounding, so nothing was changed.

Accuracy on the most recent 18 months of fights, held out from training:

| Prediction | Model | Simple baseline |
| --- | --- | --- |
| Winner | 66.0% | 59.0% (better record wins) |
| Method, when the winner is known | 51.9% | 39.9% (always unanimous decision) |
| Winner and method together | 34.8% | |
| Technique, when the finish type is known | 74.1% | 73.6% (most common technique) |

Every fight is trained on twice, once from each corner, so which corner a fighter is in
carries no information. This matters when comparing with other UFC models: ufcstats lists
the winner's corner first and the UFC books favourites in red, so **64% of fights in the
raw data are won by the first-listed corner**. A model trained on those rows as they come
scores in the high sixties before it has looked at a single statistic, and cannot be used
to predict a fight, because you would have to know which fighter to call red.

Win probabilities are close to calibrated as they come out of the blend, and are used as
they come. Isotonic and Platt calibration were both tried and both made log loss worse on
cross-validation: the model is mildly under-confident, but by an amount that moves from
one period to the next, so a correction fitted on past fights misleads on future ones.

The finishing technique barely beats the base rate, because most knockouts are punches and
most submissions are rear-naked chokes. It is never counted towards the model's record: the
scorecard keeps it in its own block, marked as decoration. Neither data source separates KO
from TKO.

If the fight data stops arriving — upstream moves, a download keeps failing — the boards
carry on drawing from whatever was last downloaded, which looks exactly like everything
working. So once the newest card has been missing for four days the bot says so once, in
the same channel as the rest of its news, and says what still works meanwhile.

### When ESPN stops answering

Cards, fighters, results and live coverage all come from one undocumented feed, so if it
goes away most of the bot goes quiet without saying why. The client records how every host
is answering, and the hourly pass posts a warning to the announcements channel once ESPN
has returned **nothing for three hours across at least twenty attempts**.

Both thresholds exist to keep it quiet. ESPN drops connections and returns 500s most days,
so a bad minute is not an outage however many requests fail inside it; and a bot with no
card to look at makes almost no requests, so hours of silence prove nothing on their own.
A single answer of any kind resets both counts.

What counts as an answer matters as much. A 404 is a failed request and a working server —
retired fighters have no profile page and the bot asks for them constantly — so a 404, and
any other refusal, clears the failure run rather than adding to it. Only a connection that
never opens, or a 5xx that survives every retry, is silence.

The warning says how long, how many attempts, what still works and what does not. It also
checks a second host: if the odds API is answering while ESPN is not, the trouble is at
ESPN's end rather than with the machine the bot runs on, and the warning says which. When
ESPN comes back the bot posts an all-clear, so the warning is never left standing after the
problem has gone.

### Ratings boards

The ratings board ranks whoever has fought in the last eighteen months and has at least
three UFC fights, by the same rating. `/ufc channels set womens_divisions:False` leaves the
women's divisions out entirely, boards and pound-for-pound alike. A fighter's division is
the one their most recent fight was made at, so a move up shows the week it happens, and a
catchweight leaves it alone. The women's divisions are kept separate from the men's.

It is not the UFC's ranking and will not agree with it. Nobody votes, holding a belt counts
for nothing by itself, and a fighter arriving from another promotion starts level with
everyone else however good they already are. Everyone starts at 1000; the number only ever
means something next to another fighter's, so where it starts is a matter of taste. Read it
as who has done the most against the best, not as a title picture: champions often sit
below contenders here, and that is the method working, not failing.

Two things stop a raw rating from reading as a ranking, and both are handled when the board
is built rather than in the rating itself. `Ledger.elo` stays exactly as the fights left it,
so nothing here changes what the model trains or predicts on.

**A rating fades while nobody is defending it.** A rating is what a fighter has earned, and
a fighter who is not fighting is not earning. Left alone, someone who retires keeps the
number they walked away with and outranks everyone still competing for it — the two-year
board had a retired heavyweight rated 126 points above the man who actually held the
division. So nothing happens for a year, which covers any ordinary gap between bouts,
injuries included; past that, what a fighter holds over the starting rating halves for
every further year out, and past eighteen months they are off the board altogether. Only
the margin fades, so sitting still can never drag anyone below where they began.

**Ratings too close to separate share a rank,** marked `=`. Across the divisional boards
the median gap between neighbours is under four points, and a single result moves a rating
by up to 32, so most adjacent pairs are inside the noise of one fight. Sorting those into
1st and 2nd claims a precision the number does not have. Fighters within five points share
a place instead, on the board and on the fighter card, which reads "joint 4th".

Records on the board count UFC fights only, which is all the dataset has: a fighter with a
long road career shows fewer wins here than their MMA record. A rating also travels with a
fighter across a division change, earned against whoever they have actually faced.

Only the last board posted carries the explanation, so the channel says it once rather than
a dozen times.

When a board moves, the move is posted to the live channel with its reason: a fighter's own
win or loss, a long layoff pulling their rating down, eighteen months without a fight, or
somebody else's result pushing them along. Only
the divisional boards are watched -- a server that leaves the women's divisions out has a
different pound-for-pound list from one that does not, so there is no single set of changes
to announce for that one.

### Keeping data current

Fight statistics come from [Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats),
which republishes ufcstats.com data once a day, in one go, at about 18:04 UTC. The bot
checks for changes using ETags, validates each download in a staging folder before using
it, then rebuilds career stats and retrains. While the newest card is missing it checks
every hour rather than waiting, since an unchanged check is four conditional requests
that come back "not modified"; ratings usually move within a couple of hours of upstream
publishing, and the move is announced as soon as the retrain finishes.

That whole job runs in a process of its own, started when it is needed and gone when it
finishes. It leaves two files behind, and the bot picks them up and swaps them in:

| File | What it holds |
| --- | --- |
| `data/career.pkl` | Every fighter's career totals and tale of the tape |
| `models/ufc_model.pkl` | The trained model as plain numbers: tree nodes and coefficients |

### Memory

The bot holds about 66 MB at rest, and roughly 95 MB is the ceiling:

| What | Cost |
| --- | --- |
| Python, aiohttp and discord.py | 47 MB |
| The rest of the bot's own code | 7 MB |
| Career data for every fighter | 9 MB |
| The compiled model | 3 MB |
| Response cache | up to 24 MB |
| Headshot cache | up to 6 MB, and released between cards |
| Fighter profiles | 0.5 MB |

Parsing the dataset needs pandas and training needs scikit-learn, which between them cost
around 150 MB resident and another 100 MB while training runs — for a job that runs once a
day. So they stay in the refresh process, which peaks near 350 MB for a minute or two and
then exits, giving all of it back. `/ufc server` reports what the process actually holds,
and says so if training ever fell back into it, since that is the one way the bot keeps
those libraries for good.

Three things keep the caches honest, all of them measured rather than guessed.

**The response cache is capped by weight, not by count.** A fighter profile is 3 KB and a
fight card is 130 KB, so counting entries says almost nothing about what is held. It is
kept to 3 MB of responses against a working set of 0.6 MB, evicting whatever was least
recently read, and anything past its lifetime is dropped on a timer rather than waiting
for the cache to fill, since nothing can be served from it again.

**Fighters are held as fighters, not as the documents they came from.** An ESPN profile is
3 KB of JSON that fills in ten fields, and parsed it costs about 23 KB. Keeping the
`Fighter` instead costs 653 bytes, so the eight hundred profiles a busy day touches come to
half a megabyte rather than eighteen.

**Headshots are dropped between cards.** A twelve-fight card is 4.7 MB of faces and the cap
sits above that, so nothing is evicted part way through a card and fetched again for the
result post. But a card is four hours and the next one is a fortnight away, so anything
untouched for six hours goes; during a card every face is read twice, at the walkout and
at the result, so nothing in use ages out.

Two other things were tried and rejected. Interning JSON keys across cached responses
saves 8% of the cache for 70% slower parsing, which is not a trade worth making. Cutting
the headshot cap below a card's worth saves 2 MB and costs a re-download of every face
mid-event.

Scoring a fight does not need either library: training writes the fitted trees and
coefficients out as plain numbers, and `scorer.py` walks them with nothing but the
standard library. It is the same arithmetic, so the compiled model returns what the
fitted one returned — training checks that on real fights every time and refuses to save
a model that disagrees by more than a rounding error.

One consequence is that a machine that only runs the bot does not need pandas or
scikit-learn installed at all, as long as `data/` and `models/` are filled in by
`python train.py` somewhere else.

### Live coverage

ESPN's play-by-play marks round starts and ends and results. ESPN only publishes running
stat totals, so each round's numbers are the difference between the totals at consecutive
round ends. Each update is recorded in the database, so a restart mid-card never repeats
a post.

Knockdowns and pauses are in the play-by-play too but are not posted: they arrive several
to a round, and a channel of one-line alerts buries the round stats and the result.

Fights are fought one at a time, so the poll only asks about the fight under way, the
next couple, and any that have finished without their result being posted. On a
twelve-fight card that is about three requests every fifteen seconds instead of twelve.

### Card changes

Withdrawals and replacements are not in the play-by-play, which only covers a card while
it is being fought. Instead the fights on each upcoming card are remembered and compared
with the next reading, once an hour, so a change is found whenever it happens.
Changes are posted to the live channel, or the schedule channel when there is no live
channel set.

Anything that would read as a fight being cancelled is only believed when the card read
cleanly. A response missing a fight, or missing the fighters within one, is left alone
until the next pass — the same rule that decides when a pick is dropped or a pick'em pick
is voided.

The walkout preview pairs ESPN's bio (record, age, height, weight, reach, stance and
country) with the ufcstats.com career numbers for both fighters. A fighter who is not in
the dataset yet, such as a debutant, still gets the ESPN rows.

Every live post names its card in the footer, alongside the time it was built. Channel
boards label their footer "Last updated" instead, because the bot edits those messages in
place.

## Data sources

- **ESPN's public MMA API:** calendar, fight cards, fighter profiles, results,
  play-by-play, fight stats and judges' scores.
- **[Polymarket](https://gamma-api.polymarket.com):** odds for every fight. No account or
  API key is needed.
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
  records.py            The shapes stored and passed around: settings, picks, results
  storage.py            SQLite database
  util.py               Small helpers
  cogs/
    ufc.py              Slash commands
    jobs.py             The two background loops
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
    ratings.py          Spots and announces moves on the ratings boards
    pickem.py           Pick'em rules: scoring, locks, saving and grading picks
  ui/
    pickem.py           Pick'em buttons and the private picker
  embeds/
    common.py           Colours, limits and text helpers
    cards.py            Fight cards, the schedule and card changes
    fighters.py         The fighter profile card
    picks.py            Picks, prediction, recap, scorecard and model status
    ratings.py          Ratings boards and their moves
    health.py           What the bot says when something it depends on breaks
    events.py           Text for Discord scheduled events
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
    rankings.py         Division and pound-for-pound ratings boards
    prediction.py       A prediction and how it is put together
```

`dataset.py` and `model.py` are the only modules that use pandas and
scikit-learn, and only `worker.py` imports them. See
[Memory](#memory) for why that matters.

`records.py` holds the dataclasses and `storage.py` the database that reads and
writes them, so the embeds and the buttons can name a shape without importing a
SQLite driver and the schema behind it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

252 tests, a few seconds, no network and no Discord: they run against a real SQLite
database in a temporary directory and fight cards built by hand. Most of them are about
what happens when a card changes underneath the bot, because that is where the awkward
cases live -- a fighter replaced, a fight cancelled, a card that only half-loaded -- and
where a mistake either shows a pick for a fight nobody is having or throws away a card's
picks over one bad response.

| File | What it covers |
| --- | --- |
| `test_tracking.py` | When a stored pick still describes a real fight, and when it does not |
| `test_pickem.py` | Scoring, and voiding picks on fights that come off the card |
| `test_cardwatch.py` | Telling a real card change from a card that did not load |
| `test_stats.py` | Divisions, ratings, rankings, the compiled scorer's arithmetic, name matching |
| `test_live_and_storage.py` | Which fights live coverage polls; database upgrades and pruning |
| `test_embeds.py` | Every board builds, stays inside Discord's limits and says the right thing |
| `test_channels.py` | What the publisher edits, re-sends and deletes; the leaderboards' lifecycle |
| `test_ratings.py` | How a ratings board is read as having moved |
| `test_espn.py` | Reading ESPN's feed, and the shapes where a field is missing |
| `test_storage.py` | The bookkeeping the rest of the bot trusts without checking |
| `test_polymarket.py` | Reading a price, and the many markets that are not one |
| `test_cogs.py` | That the cogs only reach for helpers that exist |
| `test_outage.py` | Telling an ESPN outage from a bad afternoon |

The model is not retrained here -- that takes a minute and needs the dataset. Training
checks itself instead: it scores real fights through both the fitted model and the
compiled one and refuses to save a model whose compiled form disagrees.

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
a day or more behind on withdrawals and replacements. The boards follow within about an
hour of ESPN updating; `/ufc channels refresh` only helps once it has.