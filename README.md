# UFC Discord Bot

A Discord bot for UFC fans. It follows the UFC calendar, shows fight cards and exact
ufcstats.com fighter stats, predicts every fight with a machine learning model, posts
live round-by-round coverage during cards, and keeps a public record of how its picks
have done.

## Features

- **Fight cards and schedule.** Upcoming cards, full fight cards, results and venues.
- **Fighter cards.** Three pages behind one command. The first is the page ESPN itself
  leads with — both records, division, bio, gym, headshot — plus the bot's rating and
  where it places them. Behind buttons: the career numbers ufcstats.com publishes, and
  the full fight history, which is the only page that knows about the fights before the
  UFC. Patricio Pitbull reads 38-9 professionally and 1-1 in the UFC, and the ratings
  have only ever seen the second number.
- **Predictions.** Win probability for any matchup, plus how the fight is likely to end:
  KO/TKO, submission, unanimous or split decision, with the likeliest finishing technique.
- **Picks board.** One auto-updating message per upcoming card with every pick and the
  method it is likely to come by. Fights the model cannot call — a debut has no UFC history
  to predict from — are named rather than dropped, so the card reads as complete. Picks
  lock when the card starts and are graded afterwards. No betting lines: those belong in
  pick'em.
- **Scorecard.** A running record of the model's accuracy, split by confidence, with the
  betting favourites' record over the same fights as a benchmark. It sits at the foot of
  the picks channel, because "should I believe any of this" is a question asked while
  reading the picks.
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
- **Pick'em.** Members pick winners for the next UFC card from a private menu. A right
  pick scores what a 100-point bet would win at those odds, so
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
| `/ufc fighter <name>` | Fighter profile, career stats and fight history |
| `/ufc predict <a> <b> [rounds] [title]` | Head-to-head prediction |
| `/ufc predictions [event]` | Picks for every fight on a card |
| `/ufc pickem stats [member]` | Points, rank, win rate and card history |
| `/ufc pickem picks <event>` | Everyone's picks for one card, fight by fight |
| `/ufc server` | Latency, uptime, memory and how current the data is |
| `/ufc channels set` | Choose channels for picks, schedule, live coverage, pick'em and ratings |
| `/ufc channels refresh` / `clear` | Update every board now, or stop maintaining them |
| `/ufc sync enable` / `disable` / `now` / `status` / `settings` | Discord scheduled events |
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
/ufc channels set predictions:#fight-picks schedule:#fight-calendar live:#fight-night pickem:#pickem rankings:#bot-rankings
/ufc sync enable
```

### Deploying

Pushing to `main` builds the image and restarts the container on a self-hosted runner; see
`.github/workflows/deploy.yml`. The container keeps nothing of its own, so two settings in
the server's `.env` have to point inside the mounted volume or a deploy throws them away
with the old container:

```
DATABASE_PATH=/app/data/ufcbot.sqlite3
MODEL_DIR=/app/data/models
```

The database is the only thing here that cannot be rebuilt — every member's picks, points
and leaderboard history — and it defaults to a path that is *not* on the volume.

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
| Pick'em board shares | Eight seconds after the last pick |
| Fight day reminder | Once, twelve hours before the main card |
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
new odds. The game runs on the next UFC card only, and the board turns over to it the
moment the last one ends — usually days before anyone has priced it. A fight with no line
yet is listed and says so, and cannot be picked until it has one; the rest of the card is
playable in the meantime. Each pick locks when that part of the card starts. Draws, no
contests and
cancelled fights are void, and so is a pick on a fighter who was replaced before the
bell: that fight never happened, so the pick scores nothing either way rather than
counting as a loss. A fight coming off the card is voided as soon as the bot sees it
go, rather than sitting in your picks as pending until the card is over.
Other members' picks stay hidden until the fight locks,
though the board shows the overall split. `/ufc pickem picks <event>` lays out a whole
card fight by fight afterwards: who backed whom, at what price, and what it scored them.
The channel holds three messages. None of them is ever deleted; they are edited in place,
and they keep their order:

| Message | What it is | Buttons |
| --- | --- | --- |
| **All Time Pick'em Leaderboard** | every card the bot has scored | — |
| **This / Last Card's Pick'em Leaderboard** | the card scored most recently | My picks |
| The card's pick'em board | whichever card is open now | Make your picks, My picks |

All three keep their place in the scrollback, because a message people scroll back to
should not move. The board is the part whose contents turn over, and a card ending is an
edit rather than a delete and a repost: the announcement that it happened is the fight-day
reminder and the results, which are posts of their own, so the board does not also need to
jump to the bottom to be noticed.

The board also redraws as people vote, so the share behind each fighter is current rather
than an hour old. A redraw is scheduled eight seconds after a pick and replaced by the next
one, so a member working down a twelve-fight card produces one edit once they stop, not
twelve along the way.

The card leaderboard is always about whichever card was scored most recently, so its title
turns over on its own. It reads **This Card's** from the moment the first fight of the card
being fought is graded, and goes back to **Last Card's** once the next card's board goes up
with nothing scored on it yet.

The turn happens on the last result rather than on the clock. When live coverage posts a
result it checks whether every fight on the card now has one, and if it does, the Discord
event for that card is ended there and then — within about a minute of the last fight,
instead of running on to the end time it was given days earlier. The pick'em board moves
on to the next card in the same pass.

### Fight day

Twelve hours before the **main card** — not the first bell, which can be four hours earlier
and would put the reminder at dawn — the pick'em channel says so once, with what is still
to come and how many people are playing.

It pings `@everyone`, and it is the one message where the bot allows a mention to notify,
because reaching the room is the whole point of it. Any fight that **changed** since the
board went up is named, since a pick made last week was made against a fighter who may not
be in the bout any more.

It is taken down again the moment the first fight starts. By then it is no longer a
reminder, it is a message telling people to do something they can no longer do, sitting
above the board that matters.

Each leaderboard ranks the same players three ways: **by points**, **by score** and **by
win rate**. They are not the same table. Backing every favourite wins often and loses
points and backing underdogs does the reverse, so points and score disagree; score and win
rate disagree because calling nine of twelve is a better share than calling twelve of
twenty-three. A single ranking hides whichever part of the game a player is good at.

The win rate table makes a player play before it ranks them: qualifying takes half as many
settled picks as the busiest player on the board, so one settled pick at 100% sits below
the line rather than on top of it. Half of the busiest rather than a fixed number, because
the same rule then means something on a card board and on the all-time one.

Both leaderboards also carry two extra lines at the foot, under no heading: what the model
scored on the same fights, and what backing every favourite would have scored. Neither is
ranked among the players. They pick every fight where a member picks the ones they like, and neither is
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

Two models work together. Logistic regression predicts the winner. A second model, a
blend of gradient boosting and logistic regression trained on how winners won, splits that
probability into KO/TKO, submission, unanimous decision and split decision. The likeliest finishing
technique comes from the winner's finishing history, how the opponent has been finished
before, and the league-wide rate.

**The model also rates fights that did not happen in the UFC.** ufcstats publishes UFC
fights, which is 8,935 of them, so a fighter arriving from anywhere else used to start level
with everyone — and a quarter of all fights have somebody the ratings had never seen. ESPN
publishes whole careers; walked as one Elo in date order that is **56,080 decided fights**,
and a debutant arrives with a rating earned against real opponents. It propagates, too:
beating someone who beat a UFC fighter carries through the graph, which no summary of a
record can say. Patricio Pitbull reads 1006 on the UFC rating and 1285 across 46 professional
fights.

Measured on rolling origin over 446 events, against the same model without it:

| | change in log loss | 95% interval | |
| --- | --- | --- | --- |
| fights with a debutant | −0.01114 | [−0.01731, −0.00517] | better |
| between established fighters | −0.00229 | [−0.00565, +0.00119] | no measurable change |
| overall | −0.00498 | [−0.00764, −0.00250] | better |

For scale, the largest other change this model has had was −0.0015. Eight earlier designs
that summarised the same careers into columns all failed the same way — every one helped
debutants and hurt everybody else by about as much, netting to nothing. The difference is
that the rating system walks the fights instead of being told about them afterwards.

**The graph is selected on its outcome, and that bias is real.** A regional fight is in the
data because one of the two later reached the UFC, and fighters reach the UFC by winning, so
arrivals are selected for luck as well as skill. On UFC debuts the graph over-predicts the
winner by 3.4 points — and worst for the *longest* records outside, not the shortest:

| pro fights behind the rating | n | predicted | actual | gap |
| --- | --- | --- | --- | --- |
| 0–5 | 396 | 0.434 | 0.399 | +0.035 |
| 5–10 | 861 | 0.478 | 0.472 | +0.006 |
| 10–20 | 795 | 0.506 | 0.467 | +0.040 |
| 20+ | 217 | 0.534 | 0.415 | +0.120 |

Elo accumulates and regional opponents sit near the starting rating, so the longer somebody
fought out there the more inflation they bring. That is why the *count* of professional
fights is a feature beside the rating: it lets the model fit the discount rather than have
one imposed.

`python careers.py` builds the cache and is incremental — a first run is a few thousand
requests, afterwards a handful per card. It is a separate job because the refresh runs on a
timer and this does not belong on one. **The bot runs without it**: the two features go
constant and carry nothing. And none of it reaches the boards, which say UFC fights only and
still mean it.

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

It also knows whether these two have met before. Every other input describes a fighter, or
the gap between two of them, and reads a rematch exactly like two strangers — but the one
who won last time wins again **63% of the time**, measured across the 204 rematches in the
dataset where one fighter led the series. The previous meetings are stored once per pair
rather than once per fighter, and read as they stood before the fight in question, so a
snapshot never sees a meeting that had not happened yet.

Rematches are only 2.7% of fights, so this was never going to move the headline, and it
did not: on the holdout it is worth −0.06 percentage points of accuracy, which is half a
fight in 796. Measured on the rematches alone it improves log loss from 0.609 to 0.593,
and on first meetings it changes the answer by 0.0000 — inert where it does not apply,
better calibrated where it does. Twenty-nine rematches is far too small to call it proven;
it earns its place by costing nothing everywhere else.

It is given the matchup as well as the two fighters. Every other input measures a fighter
alone, or the gap between them, which says who is the better wrestler but never whether
this wrestler can take this opponent down. So striking volume is set against the
opponent's striking defence, takedowns against takedown defence, knockdown rate against
how often the opponent has been stopped, and so on, in both directions.

The boosting and regularisation settings were searched over 108 combinations by
time-series cross-validation inside the training period, never touching the holdout.
Nothing beat what is here by more than rounding, so nothing was changed.

**The winner model used to be a blend too, and the boosted half has been dropped.** Trained
on the most recent 25, 50, 75 and 100% of history and scored on the same holdout, the two
halves pull opposite ways: the logistic half improves throughout, 0.64175 to 0.63475 in log
loss, while the boosted half gets *worse* as older fights are added, 0.64812 to 0.67697.
Blended in at the weight it had, the pair came out behind the logistic half alone — 0.63661
against 0.63475. A paired event-level bootstrap over the 117 holdout events put 77% of draws
behind removal.

Two things that nearly went wrong there are worth recording. The weight that minimises this
holdout is about 0.1, not 0 — but the holdout both picked it and scored it, so that number
is not an estimate of anything and 0 is the only point in the bowl choosable without looking
at the test set. And the boosted half degrading is about *older* data, not more of it: fresh
fights arrive at the recent end, where it was fine, so this is era-sensitivity being absorbed
as signal rather than a trend that would continue.

**The method model keeps its boosted half, at the same 0.35 the winner model used to have.**
Measured the same way and the answer came out the other way round: dropping it makes method
prediction worse, 1.1670 against 1.1633, with 87% of paired draws behind keeping it. Its own
weight sweep bottoms out around 0.25–0.30. How a half performs alone settles nothing — the
boosted half is the weaker of the two in isolation in *both* models, by 0.039 nats for the
winner and 0.024 for the method — and only one of them hurts the blend it belongs to. The two
weights are separate constants for that reason, and reconciling them without re-running both
measurements would quietly undo one of these results.

Neither interval excludes zero, so both are choices between candidates specified in advance
rather than proven results: take the option the paired evidence favours, report it as
provisional, and re-test when the evaluation can resolve differences this small.

**That re-test is `python evaluate.py`,** and it is how everything below was decided. A single
held-out tail could not settle these questions: 117 events gives a 95% interval of about
±0.005 nats on an event-level bootstrap, and the differences worth deciding are a few
thousandths. Making the holdout longer does not help, because the training set shrinks as it
grows.

So the origin walks instead. Train on everything before a block of ten cards, predict that
block, move forward, refit, and repeat until every event since 2016 has exactly one
prediction from a model that never saw it — 446 events and 5,316 fights, against 117 and
1,452. Training sets overlap between steps, but no event is scored twice, which is the
condition the bootstrap needs. The interval comes out at about ±0.0011, slightly better than
halved.

Two rules keep it honest. Hyperparameters are chosen inside each training window, on its most
recent year, and the block being predicted never takes part in choosing anything — the
validation score is optimistic, as any score used for selection is, and it is never reported.
And after selecting, the model is refitted on the *whole* window, validation year included: a
model that has not seen the fights immediately before a card is not the model the bot
deploys, and scoring one would understate what the bot does. The recent-year slice is
deliberate rather than a random sample, because its relationship to the next block is the one
the deployed model has to the next card.

**The model walks its own Elo, at a higher K than the boards.** Ratings move by `K` per
result, and predictions depend only on `K` over the divisor — so with the divisor held at 400,
`K` is the only free parameter in the rating, and the two sweeps that came before this were
exploring one dimension twice. Over 446 events, K=128 beats the boards' K=32 by 0.00145 nats,
95% interval [−0.00285, −0.00005], 97.9% of paired draws. K=64 also beats it, by 0.00091.
Both intervals clear zero, where the old holdout could not separate them at all.

Worth recording how nearly that went the other way. Letting each window pick its own K —
which is the noisier form of the question, carrying 45 separate selections — gives only
−0.00079 and straddles zero, and a short walk over 2023 onward looked conclusive at 98.2%
before the full walk cut the effect to a third of that. Fixing the grid in advance and
walking the whole period is what made it readable.

The boards keep K=32. `K` sets how widely ratings spread, so at 128 the spread roughly
triples and `TIE_GAP`, the all-time title points and the career-score divisor would all have
to be re-derived — a visible change to every board for a gain of two thousandths of a nat
that nobody reading one would see. A board wants a readable, stable ranking and the model
wants log loss; those are different jobs, so the ledger walks both ratings over the same
fights and each side reads its own. Nothing on a board moved.

**Both models keep their full training window.** Every half that ships is still improving at
100% of the data it is allowed, so there is nothing to be gained by shortening either one.
The winner model's boosted half was the only thing that preferred recent fights, and it is
the half that no longer ships.

Accuracy on the most recent 18 months of fights, held out from training:

| Prediction | Model | Simple baseline |
| --- | --- | --- |
| Winner | 65.3% | 58.9% (better record wins) |
| Method, when the winner is known | 50.4% | 38.9% (always unanimous decision) |
| Winner and method together | 33.6% | |
| Technique, when the finish type is known | 74.9% | 74.4% (most common technique) |

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

A disqualification is read as its own result rather than as nothing. It is shown with the
round it happened in, like any stoppage, but it is not a finish: the model predicts a
technique for knockouts and submissions only, and nobody finishes anybody by being
disqualified.

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

Each division gets one message, holding **Current Ratings** and, beneath it, **🐐 All Time
Ratings**. The current half ranks whoever has fought in the last eighteen months and has at
least three UFC fights. `/ufc channels set womens_divisions:False` leaves the
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

**Every fighter gets a place of their own,** and two on the same rating are separated by
who got there first — the one who has been holding that number longest goes above, on the
grounds that nobody has taken it off him since. Alphabetical order was the alternative, and
that is about their parents.

Fighters within five points used to share a place instead, marked `=`, on the argument that
a gap that small is inside the noise of a single result. The argument is sound and the
answer was not: the board still had to print one of the two above the other, so it claimed
an order and disclaimed it on the same line. A ranking that will not rank is just a ranking
with a footnote.

There was a percentage on each divisional line for a while — `56% vs Gaethje`, what these
ratings gave that fighter against the champion — on the theory that a tie band says two
fighters cannot be told apart without ever saying how far apart that is, and that a
percentage says it in a unit nobody needs a key for. It is gone. It answered in the wrong
register: a precise number standing on an imprecise one, and it read as a forecast of a
fight that is not booked. The scale it was computed on is the one the ratings are built
with, and the fights cannot tell 300 from 450 — chosen on fights before a date and scored
on the ones after, 350 is best on the held-out half at 0.68456 and 400 costs 0.00016 nats
against it. Four significant figures of nothing.

A board too long for one field gives something up rather than not posting. Past 1024
characters Discord refuses the whole embed and not the overflow, so the record goes — the
record because it is the one thing on the line that is also on the fighter's own card. The
real boards run to about 750, which a long name still clears.

**A belt is shown and never ranked on.** 🏆 holds this board's belt now, 🎖️ held it once.
The belt meant is the division's own and not any belt the fighter has: Makhachev is
welterweight champion and the lightweight boards are full of him, where the belt is
Gaethje's, so he reads as a former champion there and the current one next door. Pound for
pound counts any of them, being about no division in particular. A board that
reordered itself around the belt would be the UFC's ranking rather than this one's, but the
belt is the thing a reader already knows and looks for, and its absence beside the top name
was the question the boards kept getting asked. A champion can still sit below a contender.

Who holds a belt and how many belts someone won are two different questions, answered two
different ways. The marker follows the winner of a division's most recent title fight,
interim included; the defence count follows the undisputed chain only. The split is forced
by what the data does not say — it records that someone won a title, never that a champion
vacated, was stripped or was elevated. Aspinall vs Gane was a no contest and Aspinall then
vacated, so on the lineal chain alone the heavyweight belt sat with Jon Jones from two
years earlier, and Aspinall, whose two heavyweight titles were both interim, showed as
having never held one.

**The order is a best guess, not a measurement.** Measured across every fight on record,
100 rating points is worth about 11 points of win rate, so two fighters within about 45
points of each other are inside a 55/45 edge — and most of a divisional board sits inside
that. A
board grouped honestly at that threshold is three tiers rather than fifteen ranks, with
nine fighters sharing third at flyweight and thirteen of fifteen in one tier at
welterweight. The ranks are kept because the ordering is still the best available guess and
a board of three tiers is not one anybody can read, but the note on the board says plainly
that it is a guess.

The 400-point scale the ratings are built on is calibrated, within noise. Tested bucket by
bucket against what it predicts, in standard errors: 1.7, 0.1, 0.5, 0.2 and 1.4 across gaps
of 0-15, 15-30, 30-50, 50-80 and 80-120 points. One bucket of five at 1.7 is what chance
looks like. Log loss agrees from the other side — chosen on fights before a date and scored
on the ones after, 350 comes out best at 0.68456 and 400 costs 0.00016 nats against it, so
the fights cannot tell 300 from 450.

Two earlier attempts to fit a scale from results, 538 and 350, landed either side of it,
and a long argument was had about which was right. Both were reading noise. An implied scale
computed per bucket divides by tiny deviations near zero, which turns that 1.7-sigma bucket
into a scale of 122 and makes a flat picture look like a broken one.

**Pound for pound shares ranks within five points,** marked `=`; the divisional boards
number straight through, because each of their lines already says what the gap is worth.
The rule is the board and not whether a belt happens to be vacant — tying it to the odds
column, which disappears when a division has no champion, would have shown the same
one-point gap as a tie on one board and as two places on the one beside it.

It is presentation and nothing rests on it. It was briefly claimed here that it also kept
the ratings-move announcements quiet, by stopping a point of drift being reported as a
fighter moving up. That was measured and is wrong twice over.

A hard threshold does not remove flapping, it moves it to the boundary: a pair 4.9 points
apart is tied, 5.1 apart is not, and the split reads as a rank change although nobody
passed anybody. The layoff fade supplies the crossings for free, because it moves a
displayed rating every day with no fight behind it. Simulated over 180 days with no fights
at all, the counts come to 130 moves by shared rank against 132 by who actually passed
whom — but a net of two is not a wash, and the components are what matter:

| | |
| --- | --- |
| reported as a move, nobody passed anybody | 112 |
| somebody passed somebody, not reported | 114 |
| both agree a move happened | 18 |

So of 130 reported moves, 112 were invented and 18 were real. That is a fault in the
announcements rather than in the grouping, and it is [fixed where it belongs](#ratings-moves).
Across the divisional boards the median gap between neighbours is under four points, and a
single result moves a rating by up to 32, so most adjacent pairs really are inside the noise
of one fight. That is worth knowing when reading a board, and it is why the order here is a
best guess rather than a measurement — but it is not a reason for the board to refuse to
give one.

**Under every board is the same division all time,** marked 🐐 — and it is not the board
above with the filter taken off, because a rating cannot answer that question. A rating
adds up results, so it pays for a long career over a good one, and it is transitive, so it
cannot say *beat him three times*. Left to the rating alone this board had Holloway above
Volkanovski, who beat him three times for the featherweight title, and Du Plessis above
Anderson Silva. Judged on the
rating a fighter *retired* with it is worse still, because that judges a career by its
decline: Silva went 1-6 at the end and gave back 120 points, finishing below fighters he
would have beaten in his sleep.

So the all-time boards rank on a career score: how good a fighter was, plus 15 points for
every title defence and 5 for every title won — printed on **its own two-figure scale**, six
rating points to the point, where Jones is on 99 and the bottom of a divisional board is in
the twenties.

**The score is not on the rating scale, deliberately.** It used to be, and the two numbers
sat one above the other inviting a subtraction between two different measurements: Evloev
showed 1160 on the current board and 1257 all time, and the 97 points between them are not
97 points of anything — they are mostly the fit having no starting point to climb out of,
which is the whole reason it is used. Undefeated fighters had the widest gaps (+97, +76,
+68) and Oliveira, with 36 fights, had +8. Divided by six the number cannot be read that
way at all. The divisor is fixed rather than fitted to the field, so a fighter's score does
not move when somebody else fights, and the order comes off the unrounded points — ranking
on the score itself would tie everyone within six rating points.

**It is not a mark out of a hundred,** and saying so would be claiming a ceiling the formula
does not have. Nothing caps it: whoever eventually passes Jones scores over 100, which is
the scale working rather than breaking. "Out of a hundred" also reads like an exam grade,
which would make Khabib's 57 look like a fail when it is the eighth-best career in the
sport. The one real bound is the floor at zero, which exists so that a division thin enough
to put a losing record on its all-time board shows nothing rather than a negative.

**How good he was is fitted, not accumulated.** The running rating starts everybody at 1000
and moves them a little per fight, which is the right shape for "where does he stand today"
and the wrong one for "how good was he" — a fighter who wins 70% of the time settles 147
points up, and at 32 points a fight he cannot get there in five. So every summary of that
rating pays for length. Holding the win rate *and* the quality of opposition fixed, ten more
fights were worth **+25** rating points where ten points of win rate were worth **+14**:
length counted nearly twice what winning did, on the board that is supposed to be about how
good somebody was. That is inherited from the starting point rather than from taking a
maximum, so the career best, the best run of three or five, the 90th percentile and the
plain average all came out between +14 and +27.

`stats/strength.py` fits every fighter at once instead. One number each, chosen so the whole
record is as likely as possible, with a pull toward the middle that is strong where there is
little evidence and fades as it mounts — which is also what stops an unbeaten five-fight
record running away to infinity. Measured the same way it pays **+13** for ten fights against
**+33** for ten points of win rate. Winning is worth two and a half times fighting often,
which is the way round it should be.

The boards move accordingly. Oliveira goes from first at lightweight to joint fourth, behind
Makhachev, Nurmagomedov and Benson Henderson and level with Tsarukyan, who has thirteen
fights to his thirty-seven. Topuria and Evloev reach featherweight on ten fights each, and
Chimaev middleweight. No record shorter than nine fights is in the all-time top fifteen, and
the best unbeaten five-fight career sits 34th, so it has not swung the other way.

**Defences are counted against the division they happened in.** The count is printed on
each line, because it is most of what the mark is made of and it is the other half of what
stops the two boards reading as one quantity. A career total is the wrong number there:
Jones defended at light heavyweight eleven times and at heavyweight once, and a career
twelve on the light heavyweight board credits that division with a reign it never saw. He
reads 11 there and 12 pound for pound, which counts any belt, being about no division in
particular. Makhachev splits 4 and 1, Cormier 3 and 1, Nunes 6 and 2. The score itself
stays a career score on every board — it is one question about one fighter — so only the
count moves.

That number used to be a reading of the running rating — the career best, after an average
over the best few fights before that. Both are gone, because both paid for length. The one
property they had and this does not is that a current rating could never come out above the
all-time number printed beneath it; with the mark on its own scale there is no longer a
comparison for that rule to protect.

The belt is in the score because it is what the sport settles arguments with and the only
thing the fighters are competing for. Every pairing that read wrong without it reads right
with it:

| | rating alone | career score |
| --- | --- | --- |
| Featherweight | Holloway over Volkanovski, who beat him 3-0 | Volkanovski, Holloway, Aldo |
| Middleweight | Du Plessis 1st, Silva and Adesanya outside the top 12 | Silva, Adesanya, Weidman |
| Light Heavyweight | — | Jones, Cormier, Liddell, Ortiz |
| Welterweight | — | St-Pierre, Usman, Hughes |
| All time | Jones, Makhachev, St-Pierre, Oliveira | Jones, St-Pierre, Silva, Johnson |

An all-time line carries the division or the record, not both. Fifteen of them with both
runs past the 1024 characters Discord allows in one field, which it does not refuse — it
takes the overflow into a field of its own, leaving a gap through the middle of a ranking.
Pound for pound the division is the context; on a divisional board, where there is no
division to give, the record is.

Only the undisputed belt counts. ufcstats flags three different things as title fights and
two of them are not the belt — an interim title, and the final of a Ultimate Fighter
tournament — and counting either loses a champion their own defences: Poirier and Gaethje
each won interim lightweight titles between Khabib's defences, which read his three
defences back to him as four separate reigns.

On the all-time boards a fighter is listed in the division they fought in **most**, not the
one they finished in. Otherwise St-Pierre is a middleweight on the strength of one fight
against Bisping after twenty-one at welterweight, Jones a heavyweight, and Holloway a
welterweight. 215 fighters are in that position. On the current boards the division is
still wherever they last fought, which is the right answer to a question about now.

Records on the board count UFC fights only, which is all the dataset has: a fighter with a
long road career shows fewer wins here than their MMA record. A rating also travels with a
fighter across a division change, earned against whoever they have actually faced.

Only the last board posted carries the explanation, so the channel says it once rather than
a dozen times.

### Ratings moves

A move is announced only when one of the two fighters who swapped had a fight behind it. A
board drifts on its own, because the displayed rating fades by the day a fighter is idle,
and over 180 days with nothing happening that drift produced 130 reported moves — 112 of
them nobody passing anybody, and none of them news. "Dustin Poirier is down to 6th after a
long layoff" is a true sentence the bot can emit on an arbitrary Tuesday because a rounding
boundary moved, and it reads exactly like a sentence about something that happened.

Asking only whether *anybody* fought is not enough, and is worse than saying nothing. The
UFC runs most weekends, so a gate at that level holds a week of drift and releases all of
it on the first pass after a card — where it reads as a consequence of that card. So the
question is asked per move: this fighter, or somebody they actually passed. That keeps the
knock-on, which is the half worth keeping — a fighter dropping because the man below them
won is news they had no part in.

**A fighter back from a layoff is reported by result and place, never by direction** —
"back, now 9th · after a loss". The fade comes off the moment a fighter fights, so a
returning fighter is handed their layoff back and pays for the result out of it: carrying
more than half of K — sixteen points, about fourteen months idle for a fighter two hundred
above the starting rating — they come back from a **loss** with a higher number than they
left with, and the board moves them up. Poirier is carrying twenty-one points of that today
and Dos Anjos thirty-eight.

Saying "up to 9th" of a man who just lost is wrong. Saying nothing is worse, and was the
first attempt here: it reports a returning fighter's wins and swallows their losses, which
flatters exactly the fighters least able to carry it, and "former champion loses on return"
is usually the bigger story. So the arrow goes and the sentence keeps the result.

The fighters they passed on the fade are not reported either, and that is decided against
the **raw** rating rather than the displayed one. Being overtaken by a layoff coming off is
not being overtaken by a result: a crossing counts only where the returning fighter was not
already above that man with nothing faded off. Without it, Dos Anjos losing and climbing
twenty-two points produces "Jose Aldo drops to 10th" as the fallout of a defeat — and a
returning fighter who *wins* leaks the same way, since Poirier coming back is twenty-one
points of fade plus sixteen of result and everyone inside the first twenty-one was passed
by the calendar. Comparing raw ratings handles a win and a loss identically, because a
defeat cannot take a raw rating up past anybody.

The raw rating is kept in `ranking_state` for exactly this and is never shown. Where it is
missing — a row written before it was stored — nothing is said about that crossing at all,
rather than falling back to the ranks the board showed, which would be reading the fade as
the answer. The column is nullable for the same reason: zero is a rating a fighter could
hold, so a sentinel of zero would read to any later code that forgot to check as somebody
below everybody, and count every crossing against them.

One rule covers the fighter who was below the published places, the one who was off the
list entirely, and the row written before the raw rating was kept: a fighter who fought and
has no usable previous position is placed at the rating he carried into that fight. The
ledger keeps that now — it holds the rating after a fight, and the delta is gone, so the
replay records it on the way past.

**The watcher reads the whole division, not the published fifteen.** Crossing the line the
boards cut at is then an ordinary crossing between the fighters either side of it, rather
than a case needing rules of its own — which is what entering and leaving used to be, with
the asymmetry that fifteenth losing and dropping off was announced while the fighter who
replaced him was not. Only moves touching the published places are reported, so a shuffle
at fortieth is seen and not mentioned.

It also means a fighter who faded off the bottom still has a position to be compared against
when he comes back. For the one who does not — gone past the eighteen-month cutoff, so on no
list at all — the rating he carried into that fight stands in, which is kept on the ledger
for exactly this.

Simulated over 180 days with no fights at all, the announcements go from 130 to 4, and all
four are fighters ageing out at eighteen months.

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

Every live post names its card above the title, where it reads as which night this is
rather than as a note about the message. Channel boards label their footer "Last updated"
instead, because the bot edits those messages in place.

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
careers.py              Fetch professional careers from ESPN for the model
evaluate.py             Score the model by rolling origin, and pick its Elo K
ufcbot/
  bot.py                Bot setup and shared services
  config.py             Settings from .env
  instance.py           Prevents two copies running at once
  models.py             Event, Bout and Fighter
  records.py            The shapes stored and passed around: settings, picks, results
  schema.py             Every table, index and added column
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
    channels.py         Every auto-updating board, and what it takes to move one
    live.py             Live fight coverage
    cardwatch.py        Spots and announces changes to upcoming cards
    ratings.py          Spots and announces moves on the ratings boards
    pickem.py           Pick'em rules: scoring, locks, saving and grading picks
  ui/
    pickem.py           Pick'em buttons and the private picker
    fighter.py          The buttons under a fighter card
  embeds/
    common.py           Colours, limits and text helpers
    cards.py            Fight cards, the schedule and card changes
    fighters.py         The fighter card: overview, stats and fight history
    picks.py            Picks, prediction and scorecard embeds
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
    strength.py         How good a fighter was, fitted from every fight at once
    graph.py            Every professional fight, not just the UFC ones
    rolling.py          Rolling-origin evaluation: every event scored once
    rankings.py         Division and pound-for-pound ratings boards
    prediction.py       A prediction and how it is put together
```

`dataset.py` and `model.py` are the only modules that use pandas and
scikit-learn, and only `worker.py` imports them. See
[Memory](#memory) for why that matters.

`records.py` holds the dataclasses and `storage.py` the database that reads and
writes them, so the embeds and the buttons can name a shape without importing a
SQLite driver and the schema behind it. `schema.py` is the declaration on its own
-- what is stored, and the columns added since the first release -- because that
is read to add a column, not to follow a query.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

328 tests, a few seconds, no network and no Discord. One file per module it covers: they run against a real SQLite
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
| `test_live.py` | Which fights live coverage polls while a card is on |
| `test_embeds.py` | Every board builds, stays inside Discord's limits and says the right thing |
| `test_channels.py` | What the publisher edits, re-sends and deletes; the leaderboards' lifecycle |
| `test_ratings.py` | How a ratings board is read as having moved |
| `test_espn.py` | Reading ESPN's feed, and the shapes where a field is missing |
| `test_storage.py` | The bookkeeping the rest of the bot trusts without checking; upgrades and pruning |
| `test_polymarket.py` | Reading a price, and the many markets that are not one |
| `test_cogs.py` | That the cogs only reach for helpers that exist |
| `test_http.py` | What the response cache keeps, and telling an outage from a bad afternoon |
| `test_images.py` | The headshot cache, the only thing the bot holds in megabytes |
| `test_util.py` | The small shared helpers |

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

**A fighter has no pick or stats.** Every input the model has is built from a fighter's
UFC record, so a debutant leaves it nothing to work from — not a low confidence, no opinion
at all. Those fights still appear on the picks board, in their place on the card, reading
"UFC debut, no fight data yet" instead of a pick. The same goes for a short-notice
replacement: the fight keeps its place and loses its pick until the new fighter has UFC
history.

**A card still shows a fight that was changed.** Fight cards come from ESPN, which can be
a day or more behind on withdrawals and replacements. The boards follow within about an
hour of ESPN updating; `/ufc channels refresh` only helps once it has.