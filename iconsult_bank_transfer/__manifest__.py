{
    'name': 'Bank to Bank Transfers',
    'version': '19.0.1.1.0',
    'category': 'Accounting/Accounting',
    'summary': 'Book both legs of an internal bank transfer from one statement line',
    'description': """
Adds a "New Bank Transfer" dialog (from bank, to bank, amount: both legs booked at once;
an imported statement line later takes over the leg) and a "Bank Transfer" action on
statement lines in the reconciliation screen.
The line is booked against the Liquidity Transfer account, the matching line on the
other bank is found (or, for journals without imported statements, created) and both
legs are reconciled together. Cross-currency transfers post the exchange difference;
transfers whose other leg is not imported yet stay pending and are matched
automatically when that statement arrives.
""",
    'author': 'iConsult',
    'license': 'LGPL-3',
    'depends': ['account_accountant'],
    'data': [
        'security/ir.model.access.csv',
        'security/bank_transfer_security.xml',
        'data/ir_sequence.xml',
        'wizard/bank_transfer_direct_wizard_views.xml',
        'views/bank_transfer_views.xml',
        'views/account_journal_views.xml',
        'wizard/bank_transfer_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'iconsult_bank_transfer/static/src/**/*',
        ],
    },
    'post_init_hook': '_post_init_hook',
    'installable': True,
}
