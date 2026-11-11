import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from services.storage.json_storage import JSONStorageBackend

class AccountStorageAtomicTests(unittest.TestCase):
    def test_interrupted_replace_preserves_previous_accounts(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'accounts.json';store=JSONStorageBackend(path)
            store.save_accounts([{'id':'before'}])
            with patch('os.replace',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):store.save_accounts([{'id':'after'}])
            self.assertEqual([{'id':'before'}],store.load_accounts())
            self.assertEqual([path],list(Path(d).glob('*')))

if __name__=='__main__':unittest.main()
