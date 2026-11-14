from odoo import api, models


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    @api.model_create_multi
    def create(self, vals_list):
        st_lines = super().create(vals_list)
        if not self.env.context.get('bt_skip_automatch'):
            self.env['baho.bank.transfer']._bt_match_pending(st_lines)
        return st_lines
