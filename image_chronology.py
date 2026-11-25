"""Persist addition dates; rebuilding, editing and renaming do not make images new."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value: str) -> float:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Addition dates must include a timezone")
    return date.timestamp()


def iso_date(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds")


class ImageChronology:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "image-added-times.json"
        self.records: dict[str, dict] = {}
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("schemaVersion") != 1 or not isinstance(document.get("images"), dict):
                raise ValueError("Unsupported image addition-time ledger")
            self.records = document["images"]
            for record in self.records.values():
                timestamp(record["addedAt"])
        self.by_hash: dict[str, dict] = {}
        for record in self.records.values():
            self.remember_hash(record)
        self.imports = self.import_dates()
        self.git_paths, self.git_blobs = self.git_dates()

    def remember_hash(self, record: dict) -> None:
        digest = record.get("sha256")
        previous = self.by_hash.get(digest)
        if digest and (previous is None or timestamp(record["addedAt"]) < timestamp(previous["addedAt"])):
            self.by_hash[digest] = record

    def import_dates(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for manifest_path in sorted((self.root / "imports").glob("*.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                continue
            entries = [entry for entry in manifest.get("images", []) if isinstance(entry, dict)
                       and entry.get("status") == "imported" and entry.get("image") and entry.get("sha256")]
            # Older imports did not record each copy time. Use the completed
            # download batch and its original import order, not parallel download order.
            retrieved = [timestamp(entry["retrievedAt"]) for entry in entries if entry.get("retrievedAt")]
            batch_date = manifest.get("importedAt") or (
                iso_date(max(retrieved)) if retrieved else manifest.get("sourceFetchedAt"))
            for index, entry in enumerate(entries, 1):
                date = entry.get("addedAt") or batch_date
                if not date:
                    continue
                record = {"addedAt": iso_date(timestamp(date)),
                          "addedOrder": int(entry.get("addedOrder", entry.get("index", index))),
                          "source": f"import:{manifest_path.name}"}
                digest = entry["sha256"]
                if digest not in result or timestamp(date) < timestamp(result[digest]["addedAt"]):
                    result[digest] = record
        return result

    def git_dates(self) -> tuple[dict[str, float], dict[str, float]]:
        command = ["git", "-c", "core.quotePath=false", "log", "--reverse", "--format=@@%ct",
                   "--raw", "--no-abbrev", "--no-renames", "--diff-filter=A", "--", "images"]
        try:
            history = subprocess.check_output(command, cwd=self.root, text=True, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return {}, {}
        paths, blobs = {}, {}
        date = None
        for line in history.splitlines():
            if line.startswith("@@"):
                date = float(line[2:])
            elif line.startswith(":") and date is not None and "\t" in line:
                metadata, path = line.split("\t", 1)
                blob = metadata.split()[3]
                paths[path] = min(paths.get(path, date), date)
                blobs[blob] = min(blobs.get(blob, date), date)
        return paths, blobs

    def addition(self, path: Path, sha256: str, git_blob: str) -> dict:
        relative = path.relative_to(self.root).as_posix()
        # A replacement at the same path keeps its addition date. A renamed
        # image can recover its date by content, even across a fresh checkout.
        record = self.records.get(relative) or self.by_hash.get(sha256)
        if record is None:
            candidates = []
            if sha256 in self.imports:
                candidates.append(self.imports[sha256])
            git_date = self.git_blobs.get(git_blob, self.git_paths.get(relative))
            if git_date is not None:
                candidates.append({"addedAt": iso_date(git_date), "addedOrder": 0, "source": "git_first_seen"})
            record = min(candidates, key=lambda item: timestamp(item["addedAt"])) if candidates else {
                "addedAt": iso_date(path.stat().st_mtime), "addedOrder": 0,
                "source": "filesystem_bootstrap"}
        record = {**record, "sha256": sha256}
        self.records[relative] = record
        self.remember_hash(record)
        return {key: record[key] for key in ("addedAt", "addedOrder")}

    def save(self) -> bool:
        document = {"schemaVersion": 1, "images": dict(sorted(self.records.items()))}
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if self.path.exists() and self.path.read_text(encoding="utf-8") == text:
            return False
        self.path.write_text(text, encoding="utf-8")
        return True
