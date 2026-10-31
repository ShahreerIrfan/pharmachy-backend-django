"""
backend/tests/test_p038_release_versioning.py
Architecture Decision: ADR-0038
Backend Parity Acceptance Test Suite for Release Versioning & Client Compatibility.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import Client, TestCase

from apps.release_management.models import (
    ClientCompatibilityPolicy,
    ClientPlatform,
    CompatibilityStatus,
    ReleaseManifest,
    ReleaseStatus,
)
from apps.release_management.semver import (
    SemVer,
    compare_semver,
)


class BackendTestP038ReleaseVersioning(TestCase):
    """Verifies all acceptance criteria for P038 and ADR-0038 in backend context."""

    def setUp(self):
        self.client = Client()

        self.desktop_policy = ClientCompatibilityPolicy.objects.create(
            platform=ClientPlatform.DESKTOP,
            target_version="1.2.0",
            min_supported_version="1.0.0",
            recommended_version="1.2.0",
            upgrade_grace_period_days=30,
            download_url="https://pharmacy.bd/desktop/download",
            announcement_en="Desktop POS v1.2.0 released.",
            announcement_bn="ডেস্কটপ পিওএস v১.২.০ প্রকাশিত।",
            is_active=True,
        )

        self.api_release = ReleaseManifest.objects.create(
            platform=ClientPlatform.API,
            version="2.0.0",
            min_supported_version="1.9.0",
            release_status=ReleaseStatus.ACTIVE,
            title_en="Cloud Authority API v2.0.0",
            title_bn="ক্লাউড অথরিটি এপিআই v২.০.০",
            changelog_markdown="### Added\n- P038 Release management.",
            download_url="https://pharmacy.bd/api/v2",
            sha256_checksum="b" * 64,
            is_critical=False,
        )

    def test_semver_precedence_and_comparisons(self):
        self.assertTrue(SemVer.parse("1.0.0") < SemVer.parse("1.1.0"))
        self.assertEqual(compare_semver("1.2.0", "1.2.0"), 0)
        self.assertEqual(compare_semver("0.9.0", "1.0.0"), -1)

    def test_valid_scenario_supported_client(self):
        res = self.client.get(
            "/api/v2/releases/compatibility/",
            {"platform": "DESKTOP", "client_version": "1.2.0"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], CompatibilityStatus.SUPPORTED)

    def test_invalid_input_malformed_version_rejected(self):
        res = self.client.get(
            "/api/v2/releases/compatibility/",
            {"platform": "DESKTOP", "client_version": "bad-semver"},
        )
        self.assertEqual(res.status_code, 400)

    def test_unauthorized_cashier_blocked(self):
        res = self.client.post(
            "/api/v2/releases/",
            {
                "platform": "DESKTOP",
                "version": "1.3.0",
                "min_supported_version": "1.0.0",
                "title_en": "Unauthorized Release",
                "title_bn": "অননুমোদিত রিলিজ",
            },
            content_type="application/json",
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(res.status_code, 403)

    def test_source_specific_rule_upgrade_required(self):
        res = self.client.get(
            "/api/v2/releases/compatibility/",
            {"platform": "DESKTOP", "client_version": "0.8.0", "enforce_status_code": "true"},
        )
        self.assertEqual(res.status_code, 426)
        self.assertTrue(res.json()["is_upgrade_required"])


if __name__ == "__main__":
    unittest.main()
