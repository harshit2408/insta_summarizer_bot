"""
Shared utilities bundled into both the OAuth and Google Docs Writer Lambda zips.

These modules are duplicated into each Lambda's zip by Terraform's
``archive_file`` so they import as flat top-level modules at runtime
(``import kms_helper``, ``import google_oauth``, …). Keep them stdlib-only
to preserve sub-second cold starts.
"""
