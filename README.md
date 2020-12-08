# Θ ThetaGang Θ

ThetaGang is an [IBKR](https://www.interactivebrokers.com/) trading bot for
collecting premium by selling options using "The Wheel" strategy. The Wheel
is a strategy that [surfaced on
Reddit](https://www.reddit.com/r/options/comments/a36k4j/the_wheel_aka_triple_income_strategy_explained/),
but has been used by many in the past. This bot implements a slightly
modified version of The Wheel, with my own personal tweaks.

I've been streaming most of the work on this project [on Twitch, so follow me
over there](https://www.twitch.tv/letsmakestuff).

## How it works

You should start by reading [the Reddit
post](https://www.reddit.com/r/options/comments/a36k4j/the_wheel_aka_triple_income_strategy_explained/)
to get some background.

The strategy, as implemented here, does a few things differently from the one
described in the post above. For one, it's intended to be used to augment a
typical index-fund based portfolio with specific asset allocations. For
example, you might want to use a 60/40 portfolio with SPY (S&P500 fund) and
TLT (20 year treasury fund).

You could use this tool on individual stocks, but I personally don't
recommend it because I am not smart enough to understand which stocks to buy.
That's why I just buy index funds.

ThetaGang will try to acquire your desired allocation of each stock or ETF
according to the weights you specify in the config. To acquire the positions,
the script will write puts when conditions are met (adequate buying power,
acceptable contracts are available, enough shares needed, etc).

ThetaGang will continue to roll any open option positions indefinitely, with
the only exception being ITM puts. Once puts are in the money, they will be
ignored until they expire and are execised (after which you will own the
underlying).

In the case of deep ITM calls, the bot will prefer to roll the calls to next
expiration rather than allowing the underlying to get called away. If you
don't have adequate buying power available in your account, it's possible
that the options may get exercised instead of rolling forward and the process
starts back at the beginning. Please keep in mind this may have tax
implications, but that is outside the scope of this README.

In normal usage, you would run the script as a cronjob on a daily, hourly, or
weekly basis according to your preferences.

## Requirements

The bot is based on the [ib_insync](https://github.com/erdewit/ib_insync)
library, and uses [IBC](https://github.com/IbcAlpha/IBC) for managing the API
gateway.

To use the bot, you'll need an Interactive Brokers account with a working
installation of IBC. Additionally, you'll need an installation of Python 3.8
or newer with the [`poetry`](https://python-poetry.org/) package manager.

## Installation

```shell
$ pip install thetagang
```

## Usage

```shell
$ thetagang -h
```

## Running with Docker

My preferred way for running ThetaGang is to use a cronjob to execute Docker
commands. I've built a Docker image as part of this project, which you can
use with your installation.

To run ThetaGang within Docker, you'll need to pass `config.ini` for [IBC
configuration](https://github.com/IbcAlpha/IBC/blob/master/userguide.md) and
[`thetagang.toml`](/thetagang.toml) for ThetaGang.

The easiest way to get the config files into the container is by mounting a
volume. For example, you can use the following command:

```shell
$ docker run --rm -it \
    -v ~/ibc:/ibc \
    docker.pkg.github.com/brndnmtthws/thetagang/thetagang:latest \
    --config /ibc/thetagang.toml
```

## Development

Check out the code to your local machine and install the Python dependencies:

```shell
$ poetry install
$ poetry run thetaging -h
...
```

You are now ready to make a splash! 🐳
