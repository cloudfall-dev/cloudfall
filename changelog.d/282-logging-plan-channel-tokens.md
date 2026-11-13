### Fixed

- `logging deploy`'s plan shows each receiver channel's bot token: a new `channels` field lists `{channel, tokenStored}`, and a token not in the store is in `missing` as `telegram-bot-token of <channel>`, the name `--yes` refuses with, where the plan used to say nothing was missing (#282)
