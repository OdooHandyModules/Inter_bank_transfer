import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { BankRecButtonList } from "@account_accountant/components/bank_reconciliation/button_list/button_list";

/** Adds "Bank Transfer" to the ⋮ menu of unreconciled statement lines. */
patch(BankRecButtonList.prototype, {
    get buttons() {
        const buttons = super.buttons;
        if (!this.statementLineData.is_reconciled) {
            buttons.bankTransfer = {
                label: _t("Bank Transfer"),
                action: this.openBankTransferWizard.bind(this),
                classes: "bank-transfer-btn",
            };
        }
        return buttons;
    },

    async openBankTransferWizard() {
        await this.action.doAction("iconsult_bank_transfer.action_baho_bank_transfer_wizard", {
            additionalContext: { default_st_line_id: this.statementLineData.id },
            onClose: async () => {
                // Both legs may have changed: reload the whole list, not just this line.
                await this.env.model.load();
                this.bankReconciliation.reloadChatter();
            },
        });
    },
});
