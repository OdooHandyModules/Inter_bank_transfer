from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    bt_statements_imported = fields.Boolean(
        string="Statements are imported",
        default=True,
        help="On for journals whose statements are imported from the bank: a bank transfer "
             "waits for the matching line to be imported. Off (e.g. cash): the other leg of "
             "a bank transfer is created directly in this journal.",
    )
