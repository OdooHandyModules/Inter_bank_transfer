# Inter Bank Transfer

An Odoo 19 (Enterprise) module that books internal transfers between bank and cash journals in one step,
instead of reconciling each side by hand against the Liquidity Transfer account.

## What this module does

- **New Bank Transfer** dialog (Accounting > Transactions > Bank Transfers): choose the source and
  destination journals and the amount; both legs are booked at once. When the bank statement is imported
  later, the imported line takes over the leg that was booked in advance.
- **Bank Transfer** action on a statement line in the bank reconciliation screen: the line is booked against
  the Liquidity Transfer account, the matching line on the other bank is found (or created, for journals
  without imported statements, such as cash) and both legs are reconciled together.
- Cross-currency transfers post the exchange difference; bank charges can be included in the outgoing line.
- Transfers whose other leg has not been imported yet stay pending and are matched automatically when that
  statement arrives.

## Requirements

Odoo **19.0** Enterprise (`account_accountant`).

## License

LGPL-3.
