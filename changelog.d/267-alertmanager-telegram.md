### Added

- A `LoggingStack` receiver may name an `AlertChannel`: Alertmanager delivers to it with its native `telegram_configs` and `bot_token_file`, the token delivered by `logging deploy`; an Alertmanager older than 0.26 is refused before its configuration is written (S-015)
