import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).resolve().parents[1] / 'radiometric_calibrator' / 'installer' / 'generate_payload.py'
SPEC = importlib.util.spec_from_file_location('installer_payload', SOURCE)
payload_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(payload_module)
NS = {'w': payload_module.NAMESPACE}


class InstallerPayloadTests(unittest.TestCase):
    def create_payload(self, root):
        release = root / 'release'
        (release / '_internal' / 'Qt').mkdir(parents=True)
        (release / payload_module.EXE_NAME).write_bytes(b'test executable')
        (release / '_internal' / 'Qt' / 'example.dll').write_bytes(b'test dependency')
        return release

    def test_nested_files_and_stable_component_guids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = self.create_payload(root)
            first, second = root / 'first.wxs', root / 'second.wxs'
            self.assertEqual(payload_module.generate(release, first), 2)
            payload_module.generate(release, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            tree = ET.parse(first)
            files = tree.findall('.//w:File', NS)
            self.assertEqual(len(files), 2)
            self.assertTrue(any(item.get('Id') == 'ApplicationExecutable' for item in files))
            self.assertTrue(all(item.get('KeyPath') == 'yes' for item in files))
            self.assertFalse(tree.findall('.//w:RemoveFile', NS))

    def test_rejects_wrong_payload_and_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                payload_module.generate(root, root / 'output.wxs')
            release = self.create_payload(root)
            (release / 'radiometric_calibration_coefficients.json').write_text('{}')
            with self.assertRaises(ValueError):
                payload_module.generate(release, root / 'output.wxs')

    def test_package_has_maintenance_and_no_user_data_cleanup(self):
        tree = ET.parse(SOURCE.parent / 'Package.wxs')
        package = tree.find('w:Package', NS)
        self.assertEqual(package.get('Scope'), 'perMachine')
        self.assertIsNotNone(package.find('w:MajorUpgrade', NS))
        self.assertEqual(len(package.findall('.//w:Shortcut', NS)), 2)
        self.assertFalse(tree.findall('.//w:CustomAction', NS))
        self.assertFalse(tree.findall('.//w:RemoveFile', NS))


if __name__ == '__main__':
    unittest.main()
