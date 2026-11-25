import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from image_chronology import ImageChronology, iso_date, timestamp


def hashes(path):
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class ImageChronologyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "images/Dress").mkdir(parents=True)
        (self.root / "imports").mkdir()

    def image(self, name="series-01.png", contents=b"fixture", date=1_000):
        path = self.root / "images/Dress" / name
        path.write_bytes(contents)
        os.utime(path, (date, date))
        return path

    def test_rebuild_and_image_edit_do_not_reset_addition_time(self):
        path = self.image()
        first = ImageChronology(self.root)
        addition = first.addition(path, *hashes(path))
        first.save()
        path.write_bytes(b"replacement")
        os.utime(path, (50_000, 50_000))
        rebuilt = ImageChronology(self.root)
        self.assertEqual(addition, rebuilt.addition(path, *hashes(path)))
        rebuilt.save()
        again = ImageChronology(self.root)
        again.addition(path, *hashes(path))
        self.assertFalse(again.save())

    def test_renamed_image_keeps_its_original_date(self):
        path = self.image()
        first = ImageChronology(self.root)
        addition = first.addition(path, *hashes(path))
        first.save()
        renamed = path.with_name("renamed-99.png")
        path.rename(renamed)
        os.utime(renamed, (50_000, 50_000))
        self.assertEqual(addition, ImageChronology(self.root).addition(renamed, *hashes(renamed)))

    def test_copying_ledger_to_another_checkout_preserves_dates(self):
        path = self.image()
        first = ImageChronology(self.root)
        addition = first.addition(path, *hashes(path))
        first.save()
        with tempfile.TemporaryDirectory() as other_directory:
            other = Path(other_directory)
            (other / "images/Dress").mkdir(parents=True)
            target = other / path.relative_to(self.root)
            shutil.copyfile(path, target)
            shutil.copyfile(first.path, other / first.path.name)
            self.assertEqual(addition, ImageChronology(other).addition(target, *hashes(target)))

    def test_legacy_download_batch_uses_import_order_not_parallel_download_order(self):
        a, b = self.image("series-01.png", b"a"), self.image("series-02.png", b"b")
        manifest = {"sourceFetchedAt": iso_date(500), "images": [
            {"status": "imported", "image": "images/Dress/series-01.png", "sha256": hashes(a)[0],
             "index": 1, "retrievedAt": iso_date(900)},
            {"status": "imported", "image": "images/Dress/series-02.png", "sha256": hashes(b)[0],
             "index": 2, "retrievedAt": iso_date(800)}]}
        (self.root / "imports/source.json").write_text(json.dumps(manifest))
        ledger = ImageChronology(self.root)
        first, second = ledger.addition(a, *hashes(a)), ledger.addition(b, *hashes(b))
        self.assertEqual(first["addedAt"], second["addedAt"])
        self.assertEqual(timestamp(first["addedAt"]), 900)
        self.assertLess(first["addedOrder"], second["addedOrder"])

    def test_first_git_content_appearance_survives_a_rename_before_bootstrap(self):
        path = self.image()
        base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(self.root)]
        def git(*args, date=None):
            environment = dict(os.environ, GIT_AUTHOR_NAME="Chronology Test", GIT_COMMITTER_NAME="Chronology Test",
                               GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_EMAIL="test@example.invalid")
            if date:
                environment.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
            subprocess.check_call([*base, *args], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git("init", "-q")
        git("add", "images")
        git("commit", "-qm", "initial", date="2024-01-01T00:00:00+00:00")
        renamed = path.with_name("renamed.png")
        git("mv", str(path), str(renamed))
        git("commit", "-qm", "rename", date="2025-01-01T00:00:00+00:00")
        added = ImageChronology(self.root).addition(renamed, *hashes(renamed))
        self.assertEqual(timestamp(added["addedAt"]), timestamp("2024-01-01T00:00:00+00:00"))

    def test_invalid_persisted_dates_fail_instead_of_resetting_history(self):
        (self.root / "image-added-times.json").write_text(json.dumps({
            "schemaVersion": 1, "images": {"a": {"addedAt": "invalid"}}}))
        with self.assertRaises(ValueError):
            ImageChronology(self.root)


if __name__ == "__main__":
    unittest.main()
