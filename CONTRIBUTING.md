# Contributing to PrintFlow CRM

Thanks for helping improve PrintFlow CRM.

## Before opening a pull request

1. Open or reference an issue for substantial behavior changes when practical.
2. Keep persistent user data outside the application directory.
3. Never add customer data, API keys, access tokens, private URLs, server addresses, or other secrets to the repository.
4. Preserve backward compatibility with existing `%LOCALAPPDATA%\PrintFlowCRM\printflow.db` databases whenever possible.
5. Run a Python compile check on changed Python files.

```powershell
py -m py_compile PrintFlowCRM.pyw MessengerCapture.pyw SetupWizard.pyw
```

## Update safety

Changes to the installer or updater should preserve the core rule: application files are replaceable; user data is not. Updates should make a database backup before replacing app files.

## Pull requests

Describe what changed, why it changed, how it was tested, and any migration or rollback considerations.
