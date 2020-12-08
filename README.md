# Θ ThetaGang Θ

ThetaGang is an [IBKR](https://www.interactivebrokers.com/) trading bot for
collecting premium by selling options using "The Wheel" strategy. The Wheel
is a strategy that [surfaced on Reddit](https://www.reddit.com/r/options/comments/a36k4j/the_wheel_aka_triple_income_strategy_explained/),
but has been used by many in the past. This bot implements a slightly
modified version of The Wheel, with my own personal tweaks.

I've been streaming most of the work on this project [on Twitch, so follow me
over there](https://www.twitch.tv/letsmakestuff).

## Requirements

The bot is based on the [ib_insync](https://github.com/erdewit/ib_insync)
library, and uses [IBC](https://github.com/IbcAlpha/IBC) for managing the API
gateway.

To use the bot, you'll need an Interactive Brokers account with a working installation of IBC. Additionally, you'll need an installation of Python 3.8 or newer with the [`poetry`](https://python-poetry.org/) package manager.

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

To run ThetaGang within Docker, you'll need to pass `config.ini` for [IBC configuration](https://github.com/IbcAlpha/IBC/blob/master/userguide.md) and [`thetagang.toml`](/thetagang.toml) for ThetaGang.

The easiest way to get the config files into the container is by mounting a volume. For example, you can use the following command:

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

