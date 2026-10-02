"""Fresh install and local configuration use the same code as existing operations."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "data/test-tmp"
TEMP.mkdir(parents=True, exist_ok=True)


class FirstStartTests(unittest.TestCase):
    def run_isolated(self, data, code):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code],
            cwd=ROOT, env={**os.environ, "XIANYU_CONSOLE_DATA": str(data)},
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_first_install_can_open_bootstrap_and_empty_catalog_without_credentials(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            actual = self.run_isolated(Path(temp), """
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from console.app import create_app
from console import commerce
from console.paths import DATA
with patch('console.service.migrate', return_value={'status':'source_missing'}), patch.object(commerce, 'CATALOG', DATA/'missing-catalog.json'), patch('urllib.request.urlopen', side_effect=OSError('No browser in test')):
    with TestClient(create_app()) as client:
        bootstrap=client.get('/api/bootstrap')
        portfolio=client.get('/api/commerce')
        print(json.dumps({'bootstrap_status':bootstrap.status_code,'portfolio_status':portfolio.status_code,'products':bootstrap.json()['products'],'catalog':portfolio.json()['products'],'auth_state':bootstrap.json()['runtime']['account']['auth_state'],'messaging':bootstrap.json()['runtime']['messaging']['enabled'],'credentials':client.app.state.service.store.rows('credential')}))
""")
        self.assertEqual(actual, {"bootstrap_status": 200, "portfolio_status": 200,
            "products": [], "catalog": [], "auth_state": "not_connected", "messaging": False, "credentials": []})

    def test_local_identity_and_existing_browser_path_are_loaded_from_ignored_file(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            data = Path(temp)
            (data / "local-settings.json").write_text(json.dumps({"account": "my-local-account",
                "focus_item": "fixture-item", "managed_items": ["fixture-item"],
                "browser_profile": "data/my-existing-browser",
                "item_profiles": {"installer": "fixture-service"}}), encoding="utf-8")
            actual = self.run_isolated(data, """
import json
from console.paths import DEFAULT_ACCOUNT, DEFAULT_ITEM, MANAGED_ITEMS, BROWSER_PROFILE, PROJECT
from console.support import INSTALLER
print(json.dumps({'account':DEFAULT_ACCOUNT,'focus':DEFAULT_ITEM,'managed':sorted(MANAGED_ITEMS),'browser':BROWSER_PROFILE.relative_to(PROJECT).as_posix(),'service':INSTALLER}))
""")
        self.assertEqual(actual, {"account": "my-local-account", "focus": "fixture-item",
            "managed": ["fixture-item"], "browser": "data/my-existing-browser", "service": "fixture-service"})
