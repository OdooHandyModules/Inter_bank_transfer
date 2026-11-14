from . import models
from . import wizard


def _post_init_hook(env):
    """Cash journals have no imported statements: create their transfer leg directly."""
    env['account.journal'].search([('type', '=', 'cash')]).bt_statements_imported = False
