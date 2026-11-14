import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

# ir.config_parameter keys (with their defaults) tuning the automatic matching.
PARAM_MATCH_DAYS = ('iconsult_bank_transfer.match_days', 5)
PARAM_FX_TOLERANCE = ('iconsult_bank_transfer.fx_tolerance_pct', 2.0)


class BahoBankTransfer(models.Model):
    """One internal transfer of money between two bank/cash journals.

    Each leg is a bank statement line booked against the company's Liquidity Transfer
    account; once both legs exist, their Liquidity Transfer items are reconciled together
    (plus an exchange difference entry when the legs are in different currencies), so the
    transfer account nets to zero for every transfer.
    """
    _name = 'baho.bank.transfer'
    _description = "Bank to Bank Transfer"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(string="Reference", default='/', readonly=True, copy=False, tracking=True)
    state = fields.Selection(
        [('pending', 'Pending'), ('done', 'Done'), ('cancel', 'Cancelled')],
        default='pending', required=True, readonly=True, copy=False, tracking=True,
        help="Pending: only one leg is booked; the other is matched automatically when its "
             "statement is imported.",
    )
    # Always the main company: the two journals may belong to it or to any of its branches.
    company_id = fields.Many2one('res.company', required=True, readonly=True,
                                 default=lambda self: self.env.company.root_id)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, readonly=True, tracking=True)
    memo = fields.Char(string="Memo")

    source_journal_id = fields.Many2one(
        'account.journal', string="From", required=True, readonly=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', 'child_of', company_id)]")
    destination_journal_id = fields.Many2one(
        'account.journal', string="To", required=True, readonly=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', 'child_of', company_id)]")
    source_currency_id = fields.Many2one('res.currency', compute='_compute_currencies', store=True)
    destination_currency_id = fields.Many2one('res.currency', compute='_compute_currencies', store=True)

    amount = fields.Monetary(
        string="Amount Sent", currency_field='source_currency_id', readonly=True, tracking=True,
        help="Amount moved to the other bank, in the source currency, excluding charges.")
    charge_amount = fields.Monetary(
        string="Charges", currency_field='source_currency_id', readonly=True,
        help="Bank charges included in the outgoing statement line, booked to the charges account.")
    charge_account_id = fields.Many2one('account.account', string="Charges Account", readonly=True,
                                        check_company=True)
    destination_amount = fields.Monetary(
        string="Amount Received", currency_field='destination_currency_id', readonly=True, tracking=True)
    rate = fields.Float(string="Exchange Rate", digits=(16, 6), readonly=True)
    rate_from_currency_id = fields.Many2one('res.currency', readonly=True)
    rate_to_currency_id = fields.Many2one('res.currency', readonly=True)
    rate_display = fields.Char(string="Rate", compute='_compute_rate_display')

    source_st_line_id = fields.Many2one('account.bank.statement.line', string="Outgoing Line",
                                        readonly=True, copy=False, ondelete='set null')
    destination_st_line_id = fields.Many2one('account.bank.statement.line', string="Incoming Line",
                                             readonly=True, copy=False, ondelete='set null')
    created_st_line_id = fields.Many2one(
        'account.bank.statement.line', readonly=True, copy=False, ondelete='set null',
        help="Leg created by this transfer in a journal without imported statements.")
    exchange_move_id = fields.Many2one('account.move', string="Exchange Difference", readonly=True,
                                       copy=False)
    source_line_created = fields.Boolean(
        readonly=True, copy=False,
        help="The outgoing line was written by the transfer itself (New Bank Transfer) and will be "
             "replaced by the matching line of the next imported statement.")
    destination_line_created = fields.Boolean(
        readonly=True, copy=False,
        help="The incoming line was written by the transfer itself (New Bank Transfer) and will be "
             "replaced by the matching line of the next imported statement.")

    # -------------------------------------------------------------------------
    # COMPUTE
    # -------------------------------------------------------------------------

    @api.depends('source_journal_id', 'destination_journal_id')
    def _compute_currencies(self):
        for transfer in self:
            transfer.source_currency_id = transfer._bt_journal_currency(transfer.source_journal_id)
            transfer.destination_currency_id = transfer._bt_journal_currency(transfer.destination_journal_id)

    @api.depends('rate', 'rate_from_currency_id', 'rate_to_currency_id')
    def _compute_rate_display(self):
        for transfer in self:
            if transfer.rate and transfer.rate_from_currency_id != transfer.rate_to_currency_id:
                transfer.rate_display = f"1 {transfer.rate_from_currency_id.name} = " \
                                        f"{transfer.rate:,.4f} {transfer.rate_to_currency_id.name}"
            else:
                transfer.rate_display = False

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('company_id'):
                vals['company_id'] = self.env['res.company'].browse(vals['company_id']).root_id.id
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
                vals['name'] = self.env['ir.sequence'].with_company(company).next_by_code(
                    'baho.bank.transfer') or '/'
        return super().create(vals_list)

    @api.constrains('company_id', 'source_journal_id', 'destination_journal_id')
    def _check_journals_company(self):
        for transfer in self:
            for journal in transfer.source_journal_id | transfer.destination_journal_id:
                if journal.company_id.root_id != transfer.company_id:
                    raise ValidationError(_(
                        "The journal %(journal)s does not belong to %(company)s or one of its branches.",
                        journal=journal.display_name, company=transfer.company_id.name))

    @api.ondelete(at_uninstall=False)
    def _unlink_only_cancelled(self):
        if any(transfer.state != 'cancel' for transfer in self):
            raise UserError(_("Only cancelled bank transfers can be deleted. Cancel the transfer first."))

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    @api.model
    def _bt_journal_currency(self, journal):
        return journal.currency_id or journal.company_id.currency_id

    @api.model
    def _bt_param(self, param):
        key, default = param
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        try:
            return float(value) if value not in (None, False, '') else default
        except ValueError:
            return default

    @api.model
    def _bt_rate_currencies(self, currency_a, currency_b, company):
        """Pick how the rate is expressed: 1 <foreign> = X <company currency> when one side is
        the company currency (1 USD = 1,450 RWF rather than 1 RWF = 0.00069 USD)."""
        if currency_b == company.currency_id or currency_a != company.currency_id:
            return currency_a, currency_b
        return currency_b, currency_a

    @api.model
    def _bt_default_rate(self, from_currency, to_currency, company, date):
        if from_currency == to_currency:
            return 1.0
        return from_currency._get_conversion_rate(from_currency, to_currency, company, date)

    @api.model
    def _bt_convert(self, amount, from_currency, to_currency, rate, rate_from_currency):
        """Convert with the transfer's own rate (expressed as 1 rate_from = rate units)."""
        if from_currency == to_currency:
            return amount
        if not rate:
            raise UserError(_("An exchange rate is required for a transfer between %(a)s and %(b)s.",
                              a=from_currency.name, b=to_currency.name))
        converted = amount * rate if from_currency == rate_from_currency else amount / rate
        return to_currency.round(converted)

    def _bt_transfer_account(self):
        self.ensure_one()
        account = self.company_id.transfer_account_id
        if not account:
            raise UserError(_("Set the Liquidity Transfer account in the Accounting settings of %s.",
                              self.company_id.name))
        if not account.reconcile:
            raise UserError(_("The Liquidity Transfer account %s must allow reconciliation.",
                              account.display_name))
        return account

    def _bt_missing_leg(self):
        """'source', 'destination' or False once both legs are booked."""
        self.ensure_one()
        if not self.source_st_line_id:
            return 'source'
        if not self.destination_st_line_id:
            return 'destination'
        return False

    def _bt_expected_amount(self, leg):
        """Absolute statement amount expected on `leg`, in that leg's currency."""
        self.ensure_one()
        if leg == 'source':
            return self.source_currency_id.round(self.amount + self.charge_amount)
        return self.destination_amount

    def _bt_amount_matches(self, st_line, leg):
        self.ensure_one()
        currency = st_line.currency_id
        expected = self._bt_expected_amount(leg)
        actual = abs(st_line.amount)
        if self.source_currency_id == self.destination_currency_id:
            return float_compare(actual, expected, precision_rounding=currency.rounding) == 0
        tolerance = self._bt_param(PARAM_FX_TOLERANCE) / 100.0
        return expected and abs(actual - expected) <= expected * tolerance

    @api.model
    def _bt_st_lines_in_use(self, st_lines):
        transfers = self.search([
            ('state', '!=', 'cancel'),
            '|', ('source_st_line_id', 'in', st_lines.ids), ('destination_st_line_id', 'in', st_lines.ids),
        ])
        return transfers.source_st_line_id | transfers.destination_st_line_id

    def _bt_leg_domain(self, leg):
        """Statement lines that could be the missing `leg`, ignoring the amount."""
        self.ensure_one()
        days = int(self._bt_param(PARAM_MATCH_DAYS))
        journal = self.source_journal_id if leg == 'source' else self.destination_journal_id
        return [
            ('journal_id', '=', journal.id),
            ('is_reconciled', '=', False),
            ('amount', '<' if leg == 'source' else '>', 0),
            ('date', '>=', fields.Date.subtract(self.date, days=days)),
            ('date', '<=', fields.Date.add(self.date, days=days)),
        ]

    def _bt_find_candidates(self):
        """Unreconciled lines of the missing leg with a matching amount, closest date first."""
        self.ensure_one()
        leg = self._bt_missing_leg()
        if not leg:
            return self.env['account.bank.statement.line']
        st_lines = self.env['account.bank.statement.line'].search(self._bt_leg_domain(leg))
        st_lines -= self._bt_st_lines_in_use(st_lines)
        st_lines = st_lines.filtered(lambda line: self._bt_amount_matches(line, leg))
        return st_lines.sorted(lambda line: (abs((line.date - self.date).days), line.id))

    # -------------------------------------------------------------------------
    # BOOKING
    # -------------------------------------------------------------------------

    def _bt_book_leg(self, st_line, leg):
        """Book `st_line` on the Liquidity Transfer account (and, for the outgoing leg, the
        optional charges) in place of its suspense line."""
        self.ensure_one()
        if st_line.is_reconciled:
            raise UserError(_("The statement line %s is already reconciled.", st_line.display_name))
        expected_journal = self.source_journal_id if leg == 'source' else self.destination_journal_id
        if st_line.journal_id != expected_journal:
            raise UserError(_("The statement line %(line)s is not in the journal %(journal)s.",
                              line=st_line.display_name, journal=expected_journal.display_name))
        if (leg == 'source') != (st_line.amount < 0):
            raise UserError(_("The %(leg)s leg must be %(dir)s money.",
                              leg=_("outgoing") if leg == 'source' else _("incoming"),
                              dir=_("outgoing") if leg == 'source' else _("incoming")))

        liquidity_lines, suspense_lines, other_lines = st_line._seek_for_lines()
        if not suspense_lines or other_lines:
            raise UserError(_("The statement line %s is partially reconciled. Reset it before "
                              "using it in a bank transfer.", st_line.display_name))

        currency = suspense_lines.currency_id
        amount_currency = sum(suspense_lines.mapped('amount_currency'))
        balance = sum(suspense_lines.mapped('balance'))
        label = self._bt_line_label(leg)
        vals_list = []

        charge = self.charge_amount if leg == 'source' else 0.0
        if charge:
            if not self.charge_account_id:
                raise UserError(_("Choose an account for the bank charges."))
            if float_compare(charge, abs(amount_currency), precision_rounding=currency.rounding) >= 0:
                raise UserError(_("The charges must be lower than the statement line amount."))
            # amount_currency is positive on an outgoing line's suspense item (the debit side).
            charge_currency = currency.round(charge if amount_currency > 0 else -charge)
            charge_balance = self.company_currency_id.round(balance * charge_currency / amount_currency)
            vals_list.append({
                'name': _("%s - bank charges", self.name),
                'account_id': self.charge_account_id.id,
                'currency_id': currency.id,
                'amount_currency': charge_currency,
                'balance': charge_balance,
            })
            amount_currency -= charge_currency
            balance -= charge_balance

        vals_list.append({
            'name': label,
            'account_id': self._bt_transfer_account().id,
            'currency_id': currency.id,
            'amount_currency': amount_currency,
            'balance': balance,
        })
        st_line._set_move_line_to_statement_line_move(liquidity_lines, vals_list)
        self.write({'source_st_line_id' if leg == 'source' else 'destination_st_line_id': st_line.id})

    def _bt_line_label(self, leg):
        other = self.destination_journal_id if leg == 'source' else self.source_journal_id
        direction = _("to") if leg == 'source' else _("from")
        label = f"{self.name}: {_('Transfer')} {direction} {other.name}"
        return f"{label} - {self.memo}" if self.memo else label

    def _bt_transfer_lines(self):
        self.ensure_one()
        account = self._bt_transfer_account()
        st_lines = self.source_st_line_id | self.destination_st_line_id
        return st_lines.move_id.line_ids.filtered(lambda line: line.account_id == account)

    def _bt_create_missing_leg(self):
        """For journals without imported statements: create the other leg's statement line."""
        self.ensure_one()
        leg = self._bt_missing_leg()
        journal = self.source_journal_id if leg == 'source' else self.destination_journal_id
        amount = self._bt_expected_amount(leg)
        st_line = self.env['account.bank.statement.line'].with_context(bt_skip_automatch=True).create({
            'journal_id': journal.id,
            'date': self.date,
            'amount': -amount if leg == 'source' else amount,
            'payment_ref': self._bt_line_label(leg),
        })
        self.created_st_line_id = st_line
        return st_line

    def _bt_complete(self, st_line):
        """Book `st_line` as the missing leg, then reconcile both legs together."""
        self.ensure_one()
        leg = self._bt_missing_leg()
        if not leg or self.state != 'pending':
            raise UserError(_("The bank transfer %s is not waiting for a leg.", self.name))
        if st_line in self._bt_st_lines_in_use(st_line):
            raise UserError(_("The statement line %s is already part of another bank transfer.",
                              st_line.display_name))
        if (self.source_currency_id == self.destination_currency_id
                and not self._bt_amount_matches(st_line, leg)):
            raise UserError(_(
                "The amount of %(line)s does not match this transfer (expected %(amount)s). "
                "Same-currency transfers must match exactly; include the bank charges if the "
                "outgoing line contains them.",
                line=st_line.display_name,
                amount=self._bt_expected_amount(leg)))
        self._bt_book_leg(st_line, leg)
        self._bt_finalize()

    def _bt_book_direct(self):
        """New Bank Transfer: write both legs now and reconcile them. A leg in a journal whose
        statements are imported is flagged, so the imported line can take it over later."""
        self.ensure_one()
        StLine = self.env['account.bank.statement.line'].with_context(bt_skip_automatch=True)
        for leg in ('source', 'destination'):
            journal = self.source_journal_id if leg == 'source' else self.destination_journal_id
            amount = self._bt_expected_amount(leg)
            st_line = StLine.create({
                'journal_id': journal.id,
                'date': self.date,
                'amount': -amount if leg == 'source' else amount,
                'payment_ref': self._bt_line_label(leg),
            })
            self._bt_book_leg(st_line, leg)
            if journal.bt_statements_imported:
                self[f'{leg}_line_created'] = True
        self._bt_finalize()

    def _bt_unfinalize(self):
        """Undo the reconciliation of both Liquidity Transfer items (and the exchange entry)."""
        self.ensure_one()
        self._bt_transfer_lines().remove_move_reconcile()
        if self.exchange_move_id:
            exchange_move = self.exchange_move_id
            exchange_move.line_ids.remove_move_reconcile()
            exchange_move.button_draft()
            exchange_move.unlink()

    def _bt_adopt_imported_line(self, st_line, leg):
        """An imported statement line is the same transfer as a leg we wrote ourselves: move the
        booking onto the imported line and delete ours (the imported statement stays complete)."""
        self.ensure_one()
        ours = self.source_st_line_id if leg == 'source' else self.destination_st_line_id
        self._bt_unfinalize()
        ours.action_undo_reconciliation()
        self.write({f'{leg}_st_line_id': False, f'{leg}_line_created': False})
        ours.unlink()
        self.state = 'pending'
        self._bt_book_leg(st_line, leg)
        self._bt_finalize()
        self.message_post(body=_("Imported statement line %s replaced the line written by the transfer.",
                                 st_line.display_name))

    def _bt_finalize(self):
        """Reconcile both Liquidity Transfer items; post the exchange difference if any."""
        self.ensure_one()
        lines = self._bt_transfer_lines()
        lines.reconcile()
        residual = sum(lines.mapped('amount_residual'))
        if not self.company_currency_id.is_zero(residual):
            self.exchange_move_id = self._bt_create_exchange_move(residual)
            exchange_line = self.exchange_move_id.line_ids.filtered(
                lambda line: line.account_id == self._bt_transfer_account())
            (lines.filtered(lambda line: not line.reconciled) | exchange_line).reconcile()

        # Record what actually went through the banks.
        source_charge = self.charge_amount
        self.write({
            'state': 'done',
            'amount': self.source_currency_id.round(abs(self.source_st_line_id.amount) - source_charge),
            'destination_amount': abs(self.destination_st_line_id.amount),
        })
        self.message_post(body=_("Both legs booked and reconciled."))

    def _bt_create_exchange_move(self, residual):
        """Cancel the open `residual` (company currency) left on the transfer account."""
        self.ensure_one()
        company = self.company_id
        journal = company.currency_exchange_journal_id
        if residual > 0:
            other_account = company.expense_currency_exchange_account_id
        else:
            other_account = company.income_currency_exchange_account_id
        if not journal or not other_account:
            raise UserError(_("Configure the exchange difference journal and accounts in the "
                              "Accounting settings of %s.", company.name))
        date = max(self.source_st_line_id.date, self.destination_st_line_id.date)
        label = _("%s: exchange difference", self.name)
        currency = company.currency_id
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': date,
            'ref': label,
            'line_ids': [
                fields.Command.create({
                    'name': label,
                    'account_id': self._bt_transfer_account().id,
                    'currency_id': currency.id,
                    'amount_currency': -residual,
                    'balance': -residual,
                }),
                fields.Command.create({
                    'name': label,
                    'account_id': other_account.id,
                    'currency_id': currency.id,
                    'amount_currency': residual,
                    'balance': residual,
                }),
            ],
        })
        move._post(soft=False)
        return move

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_find_counterpart(self):
        """Pending transfer: look for the missing leg among the unreconciled lines."""
        self.ensure_one()
        if self.state != 'pending':
            return True
        candidates = self._bt_find_candidates()
        if not candidates:
            return self._bt_notify(_("No matching statement line found yet. It will be matched "
                                     "automatically when the statement is imported."), 'warning')
        if len(candidates) > 1:
            raise UserError(_(
                "Several statement lines match this transfer: %s. Open the right one in the bank "
                "reconciliation and use its Bank Transfer action to complete %s.",
                ", ".join(candidates.mapped('display_name')), self.name))
        self._bt_complete(candidates)
        return self._bt_notify(_("Matched with %s.", candidates.display_name), 'success')

    def action_cancel(self):
        """Undo the transfer: un-reconcile both legs and put the statement lines back to reconcile."""
        for transfer in self.filtered(lambda t: t.state != 'cancel'):
            st_lines = transfer.source_st_line_id | transfer.destination_st_line_id
            if st_lines:
                transfer._bt_transfer_lines().remove_move_reconcile()
            if transfer.exchange_move_id:
                exchange_move = transfer.exchange_move_id
                exchange_move.line_ids.remove_move_reconcile()
                exchange_move.button_draft()
                exchange_move.unlink()
            if st_lines:
                st_lines.action_undo_reconciliation()
            created = transfer.created_st_line_id
            if transfer.source_line_created:
                created |= transfer.source_st_line_id
            if transfer.destination_line_created:
                created |= transfer.destination_st_line_id
            transfer.write({'state': 'cancel', 'source_line_created': False, 'destination_line_created': False})
            if created:
                created.unlink()
            transfer.message_post(body=_("Transfer cancelled; its statement lines are back to reconcile."))
        return True

    def _bt_notify(self, message, kind):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'message': message, 'type': kind, 'sticky': False,
                       'next': {'type': 'ir.actions.act_window_close'}},
        }

    # -------------------------------------------------------------------------
    # AUTOMATIC MATCHING
    # -------------------------------------------------------------------------

    @api.model
    def _bt_adopt_imported(self, st_lines):
        """Imported lines that are the same transfer as a leg written by New Bank Transfer take
        that leg over. Only unambiguous pairs (one transfer, one line) are merged."""
        adopted = self.env['account.bank.statement.line']
        candidates = self.sudo().search([
            ('state', '=', 'done'),
            ('company_id', 'in', st_lines.company_id.root_id.ids),
            '|', ('source_line_created', '=', True), ('destination_line_created', '=', True),
        ], order='date, id')
        for transfer in candidates:
            for leg in ('source', 'destination'):
                if not transfer[f'{leg}_line_created']:
                    continue
                matching = st_lines.filtered_domain(transfer._bt_leg_domain(leg)) - adopted
                matching = matching.filtered(lambda line: transfer._bt_amount_matches(line, leg))
                if len(matching) != 1:
                    if matching:
                        transfer.message_post(body=_(
                            "Several imported lines could be the %(leg)s leg (%(lines)s); none was merged automatically.",
                            leg=leg, lines=", ".join(matching.mapped('display_name'))))
                    continue
                try:
                    with self.env.cr.savepoint():
                        transfer._bt_adopt_imported_line(matching, leg)
                    adopted |= matching
                except UserError as error:
                    _logger.info("Bank transfer %s: imported line %s not merged (%s)",
                                 transfer.name, matching.display_name, error)
        return adopted

    @api.model
    def _bt_match_pending(self, st_lines):
        """Called when statement lines are created (e.g. imported): complete the pending
        transfers whose missing leg is among them. A transfer is only completed when exactly
        one new line fits it, and a line is only used once."""
        st_lines = st_lines.filtered(lambda line: not line.is_reconciled and line.amount)
        if not st_lines:
            return
        st_lines -= self._bt_adopt_imported(st_lines)
        if not st_lines:
            return
        pending = self.sudo().search([
            ('state', '=', 'pending'),
            ('company_id', 'in', st_lines.company_id.root_id.ids),
            '|', ('source_journal_id', 'in', st_lines.journal_id.ids),
                 ('destination_journal_id', 'in', st_lines.journal_id.ids),
        ], order='date, id')
        used = self.env['account.bank.statement.line']
        for transfer in pending:
            leg = transfer._bt_missing_leg()
            if not leg:
                continue
            domain_lines = st_lines.filtered_domain(transfer._bt_leg_domain(leg)) - used
            candidates = domain_lines.filtered(lambda line: transfer._bt_amount_matches(line, leg))
            if len(candidates) != 1:
                continue
            try:
                with self.env.cr.savepoint():
                    transfer._bt_complete(candidates)
                used |= candidates
            except UserError as error:
                _logger.info("Bank transfer %s: automatic match with %s skipped (%s)",
                             transfer.name, candidates.display_name, error)
