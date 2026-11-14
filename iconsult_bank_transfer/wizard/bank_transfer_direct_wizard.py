from odoo import _, api, fields, models
from odoo.exceptions import UserError


class BahoBankTransferDirectWizard(models.TransientModel):
    """New Bank Transfer: pick the two banks and the amount; both legs are booked at once."""
    _name = 'baho.bank.transfer.direct.wizard'
    _description = "New Bank Transfer"
    _check_company_auto = True

    # The main company: banks and cash journals of the company and of all its branches can be used.
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company.root_id)
    source_journal_id = fields.Many2one(
        'account.journal', string="From", required=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', 'child_of', company_id)]")
    destination_journal_id = fields.Many2one(
        'account.journal', string="To", required=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', 'child_of', company_id), ('id', '!=', source_journal_id)]")
    date = fields.Date(required=True, default=fields.Date.context_today)
    source_currency_id = fields.Many2one('res.currency', compute='_compute_currencies')
    destination_currency_id = fields.Many2one('res.currency', compute='_compute_currencies')
    is_cross_currency = fields.Boolean(compute='_compute_currencies')
    amount = fields.Monetary(string="Amount Sent", currency_field='source_currency_id', required=True)

    rate_from_currency_id = fields.Many2one('res.currency', compute='_compute_rate', store=True)
    rate_to_currency_id = fields.Many2one('res.currency', compute='_compute_rate', store=True)
    rate = fields.Float(string="Exchange Rate", digits=(16, 6), compute='_compute_rate', store=True, readonly=False,
                        help="Defaults to the rate of the transfer date.")
    rate_label = fields.Char(compute='_compute_rate_label')
    destination_amount = fields.Monetary(
        string="Amount Received", currency_field='destination_currency_id',
        compute='_compute_destination_amount', store=True, readonly=False,
        help="Computed from the rate; change it if the bank credited a different amount.")

    include_charges = fields.Boolean(string="Bank charges on the outgoing side",
                                     help="Adds the charges to the amount leaving the source bank.")
    charge_amount = fields.Monetary(string="Charges", currency_field='source_currency_id')
    charge_account_id = fields.Many2one('account.account', string="Charges Account", check_company=True,
                                        domain="[('account_type', 'in', ('expense', 'expense_other'))]")
    memo = fields.Char(string="Memo")
    info = fields.Char(compute='_compute_info')

    @api.depends('source_journal_id', 'destination_journal_id')
    def _compute_currencies(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            wizard.source_currency_id = Transfer._bt_journal_currency(wizard.source_journal_id) \
                if wizard.source_journal_id else wizard.company_id.currency_id
            wizard.destination_currency_id = Transfer._bt_journal_currency(wizard.destination_journal_id) \
                if wizard.destination_journal_id else wizard.source_currency_id
            wizard.is_cross_currency = wizard.source_currency_id != wizard.destination_currency_id

    @api.depends('source_journal_id', 'destination_journal_id', 'date')
    def _compute_rate(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            if not wizard.is_cross_currency:
                wizard.rate_from_currency_id = wizard.rate_to_currency_id = wizard.source_currency_id
                wizard.rate = 1.0
                continue
            from_currency, to_currency = Transfer._bt_rate_currencies(
                wizard.source_currency_id, wizard.destination_currency_id, wizard.company_id)
            wizard.rate_from_currency_id, wizard.rate_to_currency_id = from_currency, to_currency
            wizard.rate = Transfer._bt_default_rate(from_currency, to_currency, wizard.company_id,
                                                    wizard.date or fields.Date.context_today(wizard))

    @api.depends('rate_from_currency_id', 'is_cross_currency')
    def _compute_rate_label(self):
        for wizard in self:
            wizard.rate_label = f"1 {wizard.rate_from_currency_id.name} =" if wizard.is_cross_currency else False

    @api.depends('amount', 'rate', 'source_journal_id', 'destination_journal_id')
    def _compute_destination_amount(self):
        Transfer = self.env['baho.bank.transfer']
        for wizard in self:
            if not wizard.is_cross_currency:
                wizard.destination_amount = wizard.amount
            elif wizard.rate > 0:
                wizard.destination_amount = Transfer._bt_convert(
                    wizard.amount, wizard.source_currency_id, wizard.destination_currency_id,
                    wizard.rate, wizard.rate_from_currency_id)
            else:
                wizard.destination_amount = 0.0

    @api.depends('source_journal_id', 'destination_journal_id')
    def _compute_info(self):
        for wizard in self:
            imported = (wizard.source_journal_id | wizard.destination_journal_id).filtered('bt_statements_imported')
            wizard.info = _(
                "Both banks are updated now. When the statement of %s is imported, its matching line "
                "replaces the one written here (no double counting).", ", ".join(imported.mapped('name'))
            ) if imported else False

    def action_confirm(self):
        self.ensure_one()
        if self.source_journal_id == self.destination_journal_id:
            raise UserError(_("Choose two different banks."))
        if self.amount <= 0 or self.destination_amount <= 0:
            raise UserError(_("The amounts must be positive."))
        charges = self.charge_amount if self.include_charges else 0.0
        if self.include_charges and (charges <= 0 or not self.charge_account_id):
            raise UserError(_("Enter the charges amount and the account to book them on."))
        transfer = self.env['baho.bank.transfer'].create({
            'company_id': self.company_id.id,
            'date': self.date,
            'memo': self.memo,
            'source_journal_id': self.source_journal_id.id,
            'destination_journal_id': self.destination_journal_id.id,
            'amount': self.amount,
            'charge_amount': charges,
            'charge_account_id': self.charge_account_id.id if charges else False,
            'destination_amount': self.destination_amount,
            'rate': self.rate,
            'rate_from_currency_id': self.rate_from_currency_id.id,
            'rate_to_currency_id': self.rate_to_currency_id.id,
        })
        transfer._bt_book_direct()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'baho.bank.transfer',
            'res_id': transfer.id,
            'view_mode': 'form',
            'target': 'current',
        }
