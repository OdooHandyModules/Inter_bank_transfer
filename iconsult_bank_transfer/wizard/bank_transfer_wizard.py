from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class BahoBankTransferWizard(models.TransientModel):
    """Opened from a bank statement line: books it as one leg of a bank transfer."""
    _name = 'baho.bank.transfer.wizard'
    _description = "Bank Transfer from a Statement Line"
    _check_company_auto = True

    st_line_id = fields.Many2one('account.bank.statement.line', required=True, readonly=True)
    company_id = fields.Many2one(related='st_line_id.company_id')
    journal_id = fields.Many2one(related='st_line_id.journal_id', string="This Bank")
    line_date = fields.Date(related='st_line_id.date', string="Date")
    line_label = fields.Char(related='st_line_id.payment_ref', string="Label")
    line_currency_id = fields.Many2one('res.currency', compute='_compute_line')
    line_amount = fields.Monetary(string="Amount", currency_field='line_currency_id', compute='_compute_line')
    direction = fields.Selection([('out', 'Outgoing'), ('in', 'Incoming')], compute='_compute_line')

    pending_transfer_ids = fields.Many2many('baho.bank.transfer', compute='_compute_pending_transfer_ids')
    pending_transfer_id = fields.Many2one(
        'baho.bank.transfer', string="Complete Pending Transfer",
        compute='_compute_pending_transfer_id', store=True, readonly=False,
        domain="[('id', 'in', pending_transfer_ids)]",
        help="A pending transfer this line is the missing leg of.")

    counterpart_journal_id = fields.Many2one(
        'account.journal', string="Other Bank", check_company=True,
        compute='_compute_counterpart_journal_id', store=True, readonly=False,
        domain="[('type', 'in', ('bank', 'cash')), ('id', '!=', journal_id), ('company_id', '=', company_id)]")
    counterpart_currency_id = fields.Many2one('res.currency', compute='_compute_counterpart_currency_id')
    counterpart_imported = fields.Boolean(related='counterpart_journal_id.bt_statements_imported')
    is_cross_currency = fields.Boolean(compute='_compute_counterpart_currency_id')

    rate_from_currency_id = fields.Many2one('res.currency', compute='_compute_rate', store=True)
    rate_to_currency_id = fields.Many2one('res.currency', compute='_compute_rate', store=True)
    rate = fields.Float(string="Exchange Rate", digits=(16, 6), compute='_compute_rate', store=True,
                        readonly=False, help="Defaults to the rate of the transaction date.")
    rate_label = fields.Char(compute='_compute_rate_label')

    include_charges = fields.Boolean(
        string="Charges included in the outgoing amount",
        help="Only when the outgoing statement line contains the bank charges. Leave off when "
             "the bank lists its charges as separate statement lines (the usual case).")
    charge_amount = fields.Float(string="Charges", digits=(16, 2))
    charge_account_id = fields.Many2one('account.account', string="Charges Account", check_company=True,
                                        domain="[('account_type', 'in', ('expense', 'expense_other'))]")

    expected_amount = fields.Monetary(string="Expected on the Other Bank",
                                      currency_field='counterpart_currency_id',
                                      compute='_compute_expected_amount')
    candidate_line_ids = fields.Many2many('account.bank.statement.line', compute='_compute_candidate_line_ids')
    counterpart_st_line_id = fields.Many2one(
        'account.bank.statement.line', string="Matching Line",
        compute='_compute_counterpart_st_line_id', store=True, readonly=False,
        domain="[('id', 'in', candidate_line_ids)]")
    memo = fields.Char(string="Memo")
    outcome = fields.Char(compute='_compute_outcome')

    # -------------------------------------------------------------------------
    # COMPUTE
    # -------------------------------------------------------------------------

    @api.depends('st_line_id')
    def _compute_line(self):
        for wizard in self:
            st_line = wizard.st_line_id
            wizard.line_currency_id = st_line.currency_id
            wizard.line_amount = abs(st_line.amount)
            wizard.direction = 'out' if st_line.amount < 0 else 'in'

    @api.depends('st_line_id')
    def _compute_pending_transfer_ids(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            st_line = wizard.st_line_id
            pending = Transfer.search([
                ('state', '=', 'pending'),
                ('company_id', '=', st_line.company_id.id),
                '|', ('source_journal_id', '=', st_line.journal_id.id),
                     ('destination_journal_id', '=', st_line.journal_id.id),
            ])
            wizard.pending_transfer_ids = pending.filtered(
                lambda t: t._bt_missing_leg()
                and st_line.filtered_domain(t._bt_leg_domain(t._bt_missing_leg())))

    @api.depends('pending_transfer_ids')
    def _compute_pending_transfer_id(self):
        for wizard in self:
            st_line = wizard.st_line_id
            matching = wizard.pending_transfer_ids.filtered(
                lambda t: t._bt_amount_matches(st_line, t._bt_missing_leg()))
            wizard.pending_transfer_id = matching[:1] if len(matching) == 1 else False

    @api.depends('pending_transfer_id')
    def _compute_counterpart_journal_id(self):
        for wizard in self:
            transfer = wizard.pending_transfer_id
            if transfer:
                wizard.counterpart_journal_id = (transfer.source_journal_id | transfer.destination_journal_id) \
                    - wizard.journal_id
            elif not wizard.counterpart_journal_id:
                wizard.counterpart_journal_id = False

    @api.depends('counterpart_journal_id', 'journal_id')
    def _compute_counterpart_currency_id(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            wizard.counterpart_currency_id = Transfer._bt_journal_currency(wizard.counterpart_journal_id) \
                if wizard.counterpart_journal_id else wizard.line_currency_id
            wizard.is_cross_currency = wizard.counterpart_currency_id != wizard.line_currency_id

    @api.depends('counterpart_journal_id', 'st_line_id')
    def _compute_rate(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            if not wizard.is_cross_currency:
                wizard.rate_from_currency_id = wizard.rate_to_currency_id = wizard.line_currency_id
                wizard.rate = 1.0
                continue
            from_currency, to_currency = Transfer._bt_rate_currencies(
                wizard.line_currency_id, wizard.counterpart_currency_id, wizard.company_id)
            wizard.rate_from_currency_id = from_currency
            wizard.rate_to_currency_id = to_currency
            wizard.rate = Transfer._bt_default_rate(from_currency, to_currency, wizard.company_id,
                                                    wizard.line_date)

    @api.depends('rate_from_currency_id', 'rate_to_currency_id')
    def _compute_rate_label(self):
        for wizard in self:
            wizard.rate_label = f"1 {wizard.rate_from_currency_id.name} =" if wizard.is_cross_currency else False

    def _convert(self, amount, from_currency, to_currency):
        return self.env['baho.bank.transfer']._bt_convert(
            amount, from_currency, to_currency, self.rate, self.rate_from_currency_id)

    def _net_sent(self):
        """Outgoing line: amount actually moved (line amount minus included charges)."""
        charges = self.charge_amount if self.include_charges else 0.0
        return self.line_amount - charges

    @api.depends('line_amount', 'rate', 'include_charges', 'charge_amount', 'counterpart_journal_id')
    def _compute_expected_amount(self):
        for wizard in self:
            if not wizard.counterpart_journal_id or (wizard.is_cross_currency and wizard.rate <= 0):
                wizard.expected_amount = 0.0
            elif wizard.direction == 'out':
                wizard.expected_amount = wizard._convert(
                    wizard._net_sent(), wizard.line_currency_id, wizard.counterpart_currency_id)
            else:
                sent = wizard._convert(wizard.line_amount, wizard.line_currency_id, wizard.counterpart_currency_id)
                charges = wizard.charge_amount if wizard.include_charges else 0.0
                wizard.expected_amount = wizard.counterpart_currency_id.round(sent + charges)

    @api.depends('counterpart_journal_id', 'expected_amount', 'pending_transfer_id')
    def _compute_candidate_line_ids(self):
        """Unreconciled lines of the other bank, opposite direction, within the matching window.
        Every such line is selectable; the ones with a matching amount are pre-selected."""
        StLine = self.env['account.bank.statement.line']
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            if not wizard.counterpart_journal_id or wizard.pending_transfer_id:
                wizard.candidate_line_ids = False
                continue
            days = int(Transfer._bt_param(('iconsult_bank_transfer.match_days', 5)))
            lines = StLine.search([
                ('journal_id', '=', wizard.counterpart_journal_id.id),
                ('is_reconciled', '=', False),
                ('amount', '>' if wizard.direction == 'out' else '<', 0),
                ('date', '>=', fields.Date.subtract(wizard.line_date, days=days)),
                ('date', '<=', fields.Date.add(wizard.line_date, days=days)),
            ])
            lines -= Transfer._bt_st_lines_in_use(lines)
            wizard.candidate_line_ids = lines.sorted(
                lambda line: (not wizard._amount_matches(line), abs((line.date - wizard.line_date).days)))

    def _amount_matches(self, st_line):
        currency = self.counterpart_currency_id
        actual = abs(st_line.amount)
        if not self.is_cross_currency:
            return float_compare(actual, self.expected_amount, precision_rounding=currency.rounding) == 0
        tolerance = self.env['baho.bank.transfer']._bt_param(('iconsult_bank_transfer.fx_tolerance_pct', 2.0)) / 100.0
        return bool(self.expected_amount) and abs(actual - self.expected_amount) <= self.expected_amount * tolerance

    @api.depends('candidate_line_ids')
    def _compute_counterpart_st_line_id(self):
        for wizard in self:
            matching = wizard.candidate_line_ids.filtered(wizard._amount_matches)
            wizard.counterpart_st_line_id = matching[:1] if len(matching) == 1 else False

    @api.depends('pending_transfer_id', 'counterpart_journal_id', 'counterpart_st_line_id', 'counterpart_imported')
    def _compute_outcome(self):
        for wizard in self:
            if wizard.pending_transfer_id:
                wizard.outcome = _("This line completes %s; both legs will be reconciled.",
                                   wizard.pending_transfer_id.name)
            elif not wizard.counterpart_journal_id:
                wizard.outcome = False
            elif wizard.counterpart_st_line_id:
                wizard.outcome = _("Both legs will be booked and reconciled now.")
            elif not wizard.counterpart_imported:
                wizard.outcome = _("%s has no imported statements: its leg will be created now.",
                                   wizard.counterpart_journal_id.name)
            else:
                wizard.outcome = _("No matching line on %s yet: the transfer stays Pending and is "
                                   "matched automatically when that statement is imported.",
                                   wizard.counterpart_journal_id.name)

    # -------------------------------------------------------------------------
    # ACTION
    # -------------------------------------------------------------------------

    def action_confirm(self):
        self.ensure_one()
        st_line = self.st_line_id
        if st_line.is_reconciled:
            raise UserError(_("This statement line is already reconciled."))

        if self.pending_transfer_id:
            transfer = self.pending_transfer_id
            transfer._bt_complete(st_line)
            return transfer._bt_notify(_("%s completed and reconciled.", transfer.name), 'success')

        if not self.counterpart_journal_id:
            raise UserError(_("Choose the other bank."))
        if self.is_cross_currency and self.rate <= 0:
            raise UserError(_("Enter a positive exchange rate."))
        charges = self.charge_amount if self.include_charges else 0.0
        if self.include_charges and (charges <= 0 or not self.charge_account_id):
            raise UserError(_("Enter the charges amount and the account to book them on."))

        this_currency, other_currency = self.line_currency_id, self.counterpart_currency_id
        if self.direction == 'out':
            source, destination = self.journal_id, self.counterpart_journal_id
            amount = this_currency.round(self._net_sent())
            destination_amount = self.expected_amount
        else:
            source, destination = self.counterpart_journal_id, self.journal_id
            destination_amount = self.line_amount
            amount = other_currency.round(self._convert(self.line_amount, this_currency, other_currency))

        transfer = self.env['baho.bank.transfer'].create({
            'company_id': self.company_id.id,
            'date': self.line_date,
            'memo': self.memo,
            'source_journal_id': source.id,
            'destination_journal_id': destination.id,
            'amount': amount,
            'charge_amount': charges,
            'charge_account_id': self.charge_account_id.id if charges else False,
            'destination_amount': destination_amount,
            'rate': self.rate,
            'rate_from_currency_id': self.rate_from_currency_id.id,
            'rate_to_currency_id': self.rate_to_currency_id.id,
        })
        transfer._bt_book_leg(st_line, 'source' if self.direction == 'out' else 'destination')

        if self.counterpart_st_line_id:
            transfer._bt_complete(self.counterpart_st_line_id)
            message = _("%s booked: both legs reconciled.", transfer.name)
        elif not self.counterpart_imported:
            transfer._bt_complete(transfer._bt_create_missing_leg())
            message = _("%s booked: the other leg was created and reconciled.", transfer.name)
        else:
            message = _("%s is pending until the other bank's line is imported.", transfer.name)
        return transfer._bt_notify(message, 'success')
