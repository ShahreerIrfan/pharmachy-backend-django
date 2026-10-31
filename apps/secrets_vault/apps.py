"""
Django AppConfig for apps.secrets_vault.
Architecture Decision: ADR-0037
"""

from __future__ import annotations

from django.apps import AppConfig


class SecretsVaultConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.secrets_vault"
    label = "secrets_vault"
    verbose_name = "Secrets Vault & Rotation Engine"
