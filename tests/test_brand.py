from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.app.brand import APP_NAME, APP_SLUG, APP_VERSION, ENV_PREFIX, LEGACY_ENV_PREFIX, env


class BrandTests(unittest.TestCase):
    def test_public_brand_constants(self):
        self.assertEqual(APP_NAME, "Clip Farm Pilot")
        self.assertEqual(APP_SLUG, "clipfarmpilot")
        self.assertEqual(APP_VERSION, "1.3.1")

    def test_current_environment_setting_takes_priority(self):
        with patch.dict(
            os.environ,
            {
                f"{LEGACY_ENV_PREFIX}WEB_PASSWORD": "legacy",
                f"{ENV_PREFIX}WEB_PASSWORD": "current",
            },
            clear=True,
        ):
            self.assertEqual(env("WEB_PASSWORD"), "current")

    def test_pre_rebrand_environment_setting_remains_compatible(self):
        with patch.dict(
            os.environ,
            {f"{LEGACY_ENV_PREFIX}STORAGE_DIR": "/tmp/legacy-storage"},
            clear=True,
        ):
            self.assertEqual(env("STORAGE_DIR"), "/tmp/legacy-storage")


if __name__ == "__main__":
    unittest.main()
