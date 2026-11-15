### Fixed

- Operator autonomy is now earned per environment: the history an `OperatorPolicy` grant is judged on, and the rule that suspends it after a `failed` or `interrupted` receipt, count only the receipts of servers in the policy's environment, and a receipt of a server no longer declared counts for none; before, verified runs on staging earned autonomy in production, and a failed staging run suspended production (#351)
