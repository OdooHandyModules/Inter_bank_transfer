from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBankTransfer(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.rwf = cls.company.currency_id
        cls.usd = cls.env.ref('base.USD')
        cls.transfer_account = cls.company.transfer_account_id
        cls.date = fields.Date.to_date('2026-09-10')
        # 1 USD = 1,450 RWF on the test date.
        cls.env['res.currency.rate'].create({
            'currency_id': cls.usd.id, 'company_id': cls.company.id,
            'name': cls.date, 'rate': 1 / 1450.0,
        })
        Journal = cls.env['account.journal']
        cls.bank_a = Journal.create({'name': 'BT Test Bank A', 'code': 'BTA', 'type': 'bank'})
        cls.bank_b = Journal.create({'name': 'BT Test Bank B', 'code': 'BTB', 'type': 'bank'})
        cls.bank_usd = Journal.create({'name': 'BT Test Bank USD', 'code': 'BTU', 'type': 'bank',
                                       'currency_id': cls.usd.id})
        cls.cash = Journal.create({'name': 'BT Test Cash', 'code': 'BTC', 'type': 'cash',
                                   'bt_statements_imported': False})
        cls.charge_account = cls.env['account.account'].search([
            ('account_type', '=', 'expense'), ('company_ids', 'in', cls.company.id)], limit=1)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _st_line(self, journal, amount, date=None, ref='BT test line'):
        return self.env['account.bank.statement.line'].create({
            'journal_id': journal.id, 'amount': amount, 'date': date or self.date, 'payment_ref': ref,
        })

    def _wizard(self, st_line, **vals):
        wizard = self.env['baho.bank.transfer.wizard'].create({'st_line_id': st_line.id, **vals})
        return wizard

    def _transfer_of(self, st_line):
        return self.env['baho.bank.transfer'].search([
            '|', ('source_st_line_id', '=', st_line.id), ('destination_st_line_id', '=', st_line.id)])

    def _assert_transfer_account_nets_to_zero(self, transfer):
        lines = transfer._bt_transfer_lines() | transfer.exchange_move_id.line_ids.filtered(
            lambda l: l.account_id == self.transfer_account)
        self.assertTrue(lines)
        self.assertTrue(all(lines.mapped('reconciled')), "every Liquidity Transfer item is reconciled")
        self.assertTrue(self.rwf.is_zero(sum(lines.mapped('balance'))), "Liquidity Transfer nets to zero")

    # -------------------------------------------------------------------------
    # Tests
    # -------------------------------------------------------------------------

    def test_same_currency_both_lines_imported(self):
        out_line = self._st_line(self.bank_a, -500000)
        in_line = self._st_line(self.bank_b, 500000, date=fields.Date.add(self.date, days=1))
        wizard = self._wizard(out_line, counterpart_journal_id=self.bank_b.id)
        self.assertEqual(wizard.direction, 'out')
        self.assertEqual(wizard.expected_amount, 500000)
        self.assertEqual(wizard.counterpart_st_line_id, in_line, "matching line pre-selected")
        wizard.action_confirm()

        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.destination_st_line_id, in_line)
        self.assertTrue(out_line.is_reconciled)
        self.assertTrue(in_line.is_reconciled)
        self.assertFalse(transfer.exchange_move_id)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_pending_then_matched_on_import(self):
        out_line = self._st_line(self.bank_a, -300000)
        self._wizard(out_line, counterpart_journal_id=self.bank_b.id).action_confirm()
        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.state, 'pending')
        self.assertTrue(out_line.is_reconciled, "the known leg is booked right away")

        # A line with another amount must not be matched, the right one must be.
        other = self._st_line(self.bank_b, 299000)
        self.assertEqual(transfer.state, 'pending')
        self.assertFalse(other.is_reconciled)
        in_line = self._st_line(self.bank_b, 300000, date=fields.Date.add(self.date, days=2))
        self.assertEqual(transfer.state, 'done', "matched automatically when imported")
        self.assertEqual(transfer.destination_st_line_id, in_line)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_pending_completed_from_incoming_side(self):
        out_line = self._st_line(self.bank_a, -120000)
        self._wizard(out_line, counterpart_journal_id=self.bank_b.id).action_confirm()
        transfer = self._transfer_of(out_line)
        # Two identical incoming lines in the same import: ambiguous, so no automatic match.
        in_1, in_2 = self.env['account.bank.statement.line'].create([
            {'journal_id': self.bank_b.id, 'amount': 120000, 'date': self.date, 'payment_ref': 'BT in 1'},
            {'journal_id': self.bank_b.id, 'amount': 120000, 'date': self.date, 'payment_ref': 'BT in 2'},
        ])
        self.assertEqual(transfer.state, 'pending')
        wizard = self._wizard(in_2)
        self.assertEqual(wizard.direction, 'in')
        self.assertEqual(wizard.pending_transfer_id, transfer, "the pending transfer is proposed")
        wizard.action_confirm()
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.destination_st_line_id, in_2)
        self.assertFalse(in_1.is_reconciled)

    def test_cross_currency_usd_to_rwf(self):
        out_line = self._st_line(self.bank_usd, -1000)
        in_line = self._st_line(self.bank_a, 1452000)  # bank applied 1,452 instead of 1,450
        wizard = self._wizard(out_line, counterpart_journal_id=self.bank_a.id)
        self.assertTrue(wizard.is_cross_currency)
        self.assertEqual(wizard.rate_from_currency_id, self.usd)
        self.assertAlmostEqual(wizard.rate, 1450.0, places=4)
        self.assertEqual(wizard.expected_amount, 1450000)
        self.assertEqual(wizard.counterpart_st_line_id, in_line, "within the FX tolerance")
        wizard.action_confirm()

        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.state, 'done')
        self.assertTrue(transfer.exchange_move_id, "exchange difference booked")
        gain = transfer.exchange_move_id.line_ids.filtered(
            lambda l: l.account_id == self.company.income_currency_exchange_account_id)
        self.assertEqual(gain.balance, -2000, "2,000 RWF exchange gain")
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_rwf_to_usd_from_incoming_side_with_edited_rate(self):
        in_line = self._st_line(self.bank_usd, 1000)
        out_line = self._st_line(self.bank_a, -1460000)
        wizard = self._wizard(in_line, counterpart_journal_id=self.bank_a.id)
        wizard.rate = 1460.0
        self.assertEqual(wizard.expected_amount, 1460000)
        self.assertEqual(wizard.counterpart_st_line_id, out_line)
        wizard.action_confirm()
        transfer = self._transfer_of(in_line)
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.source_journal_id, self.bank_a)
        self.assertEqual(transfer.destination_journal_id, self.bank_usd)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_charges_included_in_outgoing_line(self):
        out_line = self._st_line(self.bank_a, -205000)
        in_line = self._st_line(self.bank_b, 200000)
        wizard = self._wizard(out_line, counterpart_journal_id=self.bank_b.id, include_charges=True,
                              charge_amount=5000, charge_account_id=self.charge_account.id)
        self.assertEqual(wizard.expected_amount, 200000)
        self.assertEqual(wizard.counterpart_st_line_id, in_line)
        wizard.action_confirm()
        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.state, 'done')
        charge_line = out_line.move_id.line_ids.filtered(lambda l: l.account_id == self.charge_account)
        self.assertEqual(charge_line.balance, 5000)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_charges_off_by_default(self):
        out_line = self._st_line(self.bank_a, -205000)
        wizard = self._wizard(out_line, counterpart_journal_id=self.bank_b.id)
        self.assertFalse(wizard.include_charges)
        self.assertEqual(wizard.expected_amount, 205000)

    def test_cash_journal_leg_created(self):
        out_line = self._st_line(self.bank_a, -80000)
        wizard = self._wizard(out_line, counterpart_journal_id=self.cash.id)
        self.assertFalse(wizard.counterpart_imported)
        wizard.action_confirm()
        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.state, 'done')
        created = transfer.created_st_line_id
        self.assertEqual(created.journal_id, self.cash)
        self.assertEqual(created.amount, 80000)
        self.assertTrue(created.is_reconciled)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_cancel_restores_lines(self):
        out_line = self._st_line(self.bank_usd, -1000)
        in_line = self._st_line(self.bank_a, 1452000)
        self._wizard(out_line, counterpart_journal_id=self.bank_a.id).action_confirm()
        transfer = self._transfer_of(out_line)
        exchange_move = transfer.exchange_move_id
        self.assertTrue(exchange_move)
        transfer.action_cancel()
        self.assertEqual(transfer.state, 'cancel')
        self.assertFalse(out_line.is_reconciled)
        self.assertFalse(in_line.is_reconciled)
        self.assertFalse(exchange_move.exists(), "exchange difference entry removed")
        # Lines are free again for a new transfer.
        self._wizard(out_line, counterpart_journal_id=self.bank_a.id).action_confirm()
        self.assertEqual(self._transfer_of(out_line).filtered(lambda t: t.state != 'cancel').state, 'done')

    def test_cancel_removes_created_cash_leg(self):
        out_line = self._st_line(self.bank_a, -40000)
        self._wizard(out_line, counterpart_journal_id=self.cash.id).action_confirm()
        transfer = self._transfer_of(out_line)
        created = transfer.created_st_line_id
        transfer.action_cancel()
        self.assertFalse(created.exists())
        self.assertFalse(out_line.is_reconciled)

    def test_same_currency_amount_mismatch_refused(self):
        out_line = self._st_line(self.bank_a, -100000)
        in_line = self._st_line(self.bank_b, 99000)
        wizard = self._wizard(out_line, counterpart_journal_id=self.bank_b.id)
        self.assertFalse(wizard.counterpart_st_line_id, "not pre-selected")
        wizard.counterpart_st_line_id = in_line
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_line_used_once(self):
        out_1 = self._st_line(self.bank_a, -60000)
        in_line = self._st_line(self.bank_b, 60000)
        self._wizard(out_1, counterpart_journal_id=self.bank_b.id).action_confirm()
        self.assertEqual(self._transfer_of(in_line).state, 'done')
        out_2 = self._st_line(self.bank_a, -60000)
        wizard = self._wizard(out_2, counterpart_journal_id=self.bank_b.id)
        self.assertNotIn(in_line, wizard.candidate_line_ids, "already used by another transfer")

    def test_delete_only_cancelled(self):
        out_line = self._st_line(self.bank_a, -10000)
        self._wizard(out_line, counterpart_journal_id=self.cash.id).action_confirm()
        transfer = self._transfer_of(out_line)
        with self.assertRaises(UserError):
            transfer.unlink()
        transfer.action_cancel()
        transfer.unlink()


@tagged('post_install', '-at_install')
class TestDirectBankTransfer(TestBankTransfer):
    """New Bank Transfer dialog: both legs booked at once; imported lines take over later."""

    def _direct(self, source, destination, amount, **vals):
        wizard = self.env['baho.bank.transfer.direct.wizard'].create({
            'source_journal_id': source.id, 'destination_journal_id': destination.id,
            'amount': amount, 'date': self.date, **vals})
        action = wizard.action_confirm()
        return self.env['baho.bank.transfer'].browse(action['res_id'])

    def test_direct_same_currency(self):
        transfer = self._direct(self.bank_a, self.bank_b, 250000)
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.source_st_line_id.amount, -250000)
        self.assertEqual(transfer.destination_st_line_id.amount, 250000)
        self.assertTrue(transfer.source_st_line_id.is_reconciled and transfer.destination_st_line_id.is_reconciled)
        self.assertTrue(transfer.source_line_created and transfer.destination_line_created)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_direct_then_import_takes_over(self):
        transfer = self._direct(self.bank_a, self.bank_b, 180000)
        ours = transfer.destination_st_line_id
        imported = self._st_line(self.bank_b, 180000, date=fields.Date.add(self.date, days=1), ref='BK statement: transfer')
        self.assertFalse(ours.exists(), "our line is replaced by the imported one")
        self.assertEqual(transfer.destination_st_line_id, imported)
        self.assertTrue(imported.is_reconciled)
        self.assertFalse(transfer.destination_line_created)
        self.assertTrue(transfer.source_line_created, "the other leg still waits for its own import")
        self.assertEqual(transfer.state, 'done')
        self._assert_transfer_account_nets_to_zero(transfer)
        imported_out = self._st_line(self.bank_a, -180000, ref='BK statement: transfer out')
        self.assertEqual(transfer.source_st_line_id, imported_out)
        self.assertFalse(transfer.source_line_created)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_import_other_amount_not_merged(self):
        transfer = self._direct(self.bank_a, self.bank_b, 90000)
        ours = transfer.destination_st_line_id
        other = self._st_line(self.bank_b, 91000)
        self.assertTrue(ours.exists())
        self.assertFalse(other.is_reconciled)

    def test_ambiguous_import_not_merged(self):
        transfer = self._direct(self.bank_a, self.bank_b, 70000)
        ours = transfer.destination_st_line_id
        lines = self.env['account.bank.statement.line'].create([
            {'journal_id': self.bank_b.id, 'amount': 70000, 'date': self.date, 'payment_ref': 'dup 1'},
            {'journal_id': self.bank_b.id, 'amount': 70000, 'date': self.date, 'payment_ref': 'dup 2'}])
        self.assertTrue(ours.exists(), "two identical imported lines: nothing merged")
        self.assertFalse(any(lines.mapped('is_reconciled')))

    def test_direct_to_cash_keeps_line(self):
        transfer = self._direct(self.bank_a, self.cash, 40000)
        self.assertFalse(transfer.destination_line_created, "cash has no imported statements")
        cash_line = transfer.destination_st_line_id
        self._st_line(self.cash, 40000)
        self.assertTrue(cash_line.exists())
        self.assertEqual(transfer.destination_st_line_id, cash_line)

    def test_direct_cross_currency(self):
        transfer = self._direct(self.bank_usd, self.bank_a, 1000, destination_amount=1452000)
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.destination_st_line_id.amount, 1452000)
        self.assertTrue(transfer.exchange_move_id)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_direct_rate_defaults(self):
        wizard = self.env['baho.bank.transfer.direct.wizard'].create({
            'source_journal_id': self.bank_usd.id, 'destination_journal_id': self.bank_a.id,
            'amount': 1000, 'date': self.date})
        self.assertAlmostEqual(wizard.rate, 1450.0, places=4)
        self.assertEqual(wizard.destination_amount, 1450000)

    def test_direct_with_charges(self):
        transfer = self._direct(self.bank_a, self.bank_b, 100000, include_charges=True, charge_amount=2000,
                                charge_account_id=self.charge_account.id)
        self.assertEqual(transfer.source_st_line_id.amount, -102000)
        self.assertEqual(transfer.destination_st_line_id.amount, 100000)
        self._assert_transfer_account_nets_to_zero(transfer)

    def test_direct_cancel_removes_our_lines(self):
        transfer = self._direct(self.bank_a, self.bank_b, 30000)
        lines = transfer.source_st_line_id | transfer.destination_st_line_id
        transfer.action_cancel()
        self.assertFalse(lines.exists())
        self.assertEqual(transfer.state, 'cancel')

    def test_direct_same_bank_refused(self):
        with self.assertRaises(UserError):
            self._direct(self.bank_a, self.bank_a, 1000)


@tagged('post_install', '-at_install')
class TestBranchBankTransfer(TransactionCase):
    """Transfers between the main company and its branches (Odoo branches: same legal entity)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hq = cls.env.company.root_id
        cls.branch = cls.env['res.company'].create({'name': 'BT Test Branch', 'parent_id': cls.hq.id})
        cls.env.user.company_ids |= cls.branch
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[cls.hq.id, cls.branch.id]))
        cls.date = fields.Date.to_date('2026-09-10')
        Journal = cls.env['account.journal']
        suspense = {'suspense_account_id': cls.hq.account_journal_suspense_account_id.id}
        cls.hq_bank = Journal.create({'name': 'BT HQ Bank', 'code': 'BTHQ', 'type': 'bank',
                                      'company_id': cls.hq.id})
        cls.branch_cash = Journal.create({'name': 'BT Branch Cash', 'code': 'BTBC', 'type': 'cash',
                                          'company_id': cls.branch.id, 'bt_statements_imported': False,
                                          **suspense})
        cls.branch_bank = Journal.create({'name': 'BT Branch Bank', 'code': 'BTBB', 'type': 'bank',
                                          'company_id': cls.branch.id, **suspense})
        cls.transfer_account = cls.hq.transfer_account_id

    def _st_line(self, journal, amount, date=None, ref='BT branch line'):
        return self.env['account.bank.statement.line'].create({
            'journal_id': journal.id, 'amount': amount, 'date': date or self.date, 'payment_ref': ref,
        })

    def _transfer_of(self, st_line):
        return self.env['baho.bank.transfer'].search([
            '|', ('source_st_line_id', '=', st_line.id), ('destination_st_line_id', '=', st_line.id)])

    def _assert_done(self, transfer):
        self.assertEqual(transfer.state, 'done')
        self.assertEqual(transfer.company_id, self.hq, "transfers are kept on the main company")
        self.assertTrue(transfer.source_st_line_id.is_reconciled and transfer.destination_st_line_id.is_reconciled)
        lines = transfer._bt_transfer_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(lines.mapped('reconciled')))
        self.assertTrue(self.hq.currency_id.is_zero(sum(lines.mapped('balance'))))

    def test_branch_cash_out_to_hq_bank_pending_then_imported(self):
        cash_out = self._st_line(self.branch_cash, -150000, ref='POS cash out to HQ bank')
        self.assertEqual(cash_out.company_id, self.branch)
        wizard = self.env['baho.bank.transfer.wizard'].create({'st_line_id': cash_out.id})
        self.assertEqual(wizard.company_id, self.hq)
        self.assertIn(self.hq_bank, self.env['account.journal'].search(
            [('type', 'in', ('bank', 'cash')), ('company_id', 'child_of', wizard.company_id.id)]))
        wizard.counterpart_journal_id = self.hq_bank
        wizard.action_confirm()
        transfer = self._transfer_of(cash_out)
        self.assertEqual(transfer.state, 'pending', "HQ bank statements are imported: wait for the deposit")
        deposit = self._st_line(self.hq_bank, 150000, date=fields.Date.add(self.date, days=2), ref='Cash deposit')
        self.assertEqual(transfer.destination_st_line_id, deposit)
        self._assert_done(transfer)
        self.assertEqual(deposit.company_id, self.hq)

    def test_hq_bank_to_branch_cash_leg_created(self):
        out_line = self._st_line(self.hq_bank, -80000)
        self.env['baho.bank.transfer.wizard'].create({
            'st_line_id': out_line.id, 'counterpart_journal_id': self.branch_cash.id}).action_confirm()
        transfer = self._transfer_of(out_line)
        self.assertEqual(transfer.destination_st_line_id.journal_id, self.branch_cash)
        self.assertEqual(transfer.destination_st_line_id.company_id, self.branch)
        self._assert_done(transfer)

    def test_direct_between_branch_and_hq(self):
        wizard = self.env['baho.bank.transfer.direct.wizard'].create({
            'source_journal_id': self.branch_bank.id, 'destination_journal_id': self.hq_bank.id,
            'amount': 60000, 'date': self.date})
        self.assertEqual(wizard.company_id, self.hq)
        action = wizard.action_confirm()
        transfer = self.env['baho.bank.transfer'].browse(action['res_id'])
        self._assert_done(transfer)
        self.assertTrue(transfer.source_line_created and transfer.destination_line_created)
        imported = self._st_line(self.branch_bank, -60000, ref='Branch bank statement')
        self.assertEqual(transfer.source_st_line_id, imported, "the branch's imported line takes over")
        self._assert_done(transfer)

    def test_journal_of_another_company_refused(self):
        other = self.env['res.company'].create({'name': 'BT Other Company'})
        self.env.user.company_ids |= other
        other_bank = self.env['account.journal'].with_context(
            allowed_company_ids=[other.id] + self.env.context['allowed_company_ids']).create(
            {'name': 'BT Other Bank', 'code': 'BTOB', 'type': 'bank', 'company_id': other.id})
        with self.assertRaises(ValidationError):
            self.env['baho.bank.transfer'].create({
                'company_id': self.hq.id, 'date': self.date,
                'source_journal_id': self.hq_bank.id, 'destination_journal_id': other_bank.id,
                'amount': 1000, 'destination_amount': 1000, 'rate': 1.0})
