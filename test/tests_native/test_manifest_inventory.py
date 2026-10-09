import os
from pathlib import Path
from unittest import TestCase, skipUnless


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = ROOT / "f469-disco" / "manifests"


def load_manifest_inventory(filename):
    freeze_calls = []

    def freeze(root, modules):
        freeze_calls.append((root, tuple(modules)))

    previous_cwd = os.getcwd()
    os.chdir(MANIFEST_DIR)
    try:
        namespace = {"freeze": freeze, "include": lambda unused: None}
        exec((MANIFEST_DIR / filename).read_text(), namespace)
    finally:
        os.chdir(previous_cwd)

    if len(freeze_calls) != 1:
        raise AssertionError("expected one freeze declaration")
    return freeze_calls[0]


# These manifests arrive with the f469-disco commit that turns embit into a
# submodule. The esp32-p4-port branch pins f469-disco on its LVGL 9 line, which
# still vendors embit and has neither file, so there is nothing to check yet.
HAS_SPLIT_MANIFESTS = (MANIFEST_DIR / "common.py").exists() and (
    MANIFEST_DIR / "embit.py"
).exists()


@skipUnless(HAS_SPLIT_MANIFESTS, "f469-disco still vendors embit on this branch")
class ManifestInventoryTest(TestCase):
    def test_common_inventory_matches_source_tree(self):
        common_root = ROOT / "f469-disco" / "libs" / "common"
        expected = {
            path.relative_to(common_root).as_posix()
            for path in common_root.rglob("*.py")
            if path.relative_to(common_root).parts[0] != "embit"
        }

        freeze_root, modules = load_manifest_inventory("common.py")
        self.assertEqual(freeze_root, "../libs/common")
        self.assertEqual(set(modules), expected)
        self.assertEqual(len(modules), len(expected))
        self.assertIn("asyncio/core.py", modules)
        self.assertIn("microur/util/bytewords.py", modules)
        self.assertEqual(modules, load_manifest_inventory("common.py")[1])

    def test_embit_inventory_matches_source_tree_except_util(self):
        embit_root = ROOT / "f469-disco" / "libs" / "common" / "embit" / "src"
        expected = {
            path.relative_to(embit_root).as_posix()
            for path in embit_root.rglob("*.py")
            if path.relative_to(embit_root).parts[:2] != ("embit", "util")
        }

        freeze_root, modules = load_manifest_inventory("embit.py")
        self.assertEqual(freeze_root, "../libs/common/embit/src")
        self.assertEqual(set(modules), expected)
        self.assertEqual(len(modules), len(expected))
        self.assertIn("embit/descriptor/arguments.py", modules)
        self.assertIn("embit/liquid/slip77.py", modules)
        self.assertFalse(any(path.startswith("embit/util/") for path in modules))
        self.assertEqual(modules, load_manifest_inventory("embit.py")[1])
