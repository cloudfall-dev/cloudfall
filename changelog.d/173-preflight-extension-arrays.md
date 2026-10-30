### Fixed

- The data copy preflight now names an extension that a selected table needs through an array or a domain of the extension's type, such as a `citext[]` column or a column of a domain over `citext` (chained domains included), under `extensions` and passes it to `pg_dump --extension`; before, only a direct use of the extension's type was seen, so the copy skipped the extension and the restore failed (#173)
