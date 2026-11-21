import json
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.room_maps import RoomMapError, build_room_maps

ROOT = Path(__file__).resolve().parent.parent


class RoomMapBuildTest(unittest.TestCase):
    def test_publishes_annotated_rooms_across_buildings(self) -> None:
        annotations = json.loads(
            (ROOT / "static" / "room_map_annotations.json").read_text(encoding="utf-8")
        )
        expected = {
            room["code"]: floor
            for floor in annotations["maps"]
            for room in floor["rooms"]
        }
        rules = json.loads((ROOT / "static/building_img_rules.json").read_text())
        self.assertEqual(
            {floor["sourceImagePath"] for floor in annotations["maps"]},
            {rule["path"].removeprefix("./") for rule in rules},
        )
        for code, floor in expected.items():
            matched = next(rule for rule in rules if re.fullmatch(rule["regex"], code))
            self.assertEqual(
                floor["sourceImagePath"], matched["path"].removeprefix("./")
            )
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = Path(temporary_dir)
            sample = next(
                floor
                for floor in annotations["maps"]
                if any(room["code"] == "5201" for room in floor["rooms"])
            )
            annotation_path = output_dir / "annotations.json"
            annotation_path.write_text(json.dumps({"version": 1, "maps": [sample]}))
            manifest_path = build_room_maps(
                ROOT / "static", output_dir, annotations_path=annotation_path
            )
            rooms = json.loads(manifest_path.read_text())["rooms"]
            self.assertEqual(
                {room["code"] for room in rooms},
                {room["code"] for room in sample["rooms"]},
            )
            room = next(room for room in rooms if room["code"] == "5201")
            with (
                Image.open(ROOT / "static" / room["sourceImagePath"]) as source,
                Image.open(output_dir / room["imagePath"]) as rendered,
            ):
                source = source.convert("RGB")
                self.assertEqual(rendered.size, source.size)
                self.assertNotEqual(
                    rendered.getpixel((1700, 640)), source.getpixel((1700, 640))
                )
                self.assertEqual(
                    rendered.getpixel((1400, 640)), source.getpixel((1400, 640))
                )

    def test_rejects_source_image_size_changes(self) -> None:
        annotations = json.loads(
            (ROOT / "static" / "room_map_annotations.json").read_text(encoding="utf-8")
        )
        annotations["maps"][0]["sourceImageSize"] = [1, 1]
        with tempfile.TemporaryDirectory() as temporary_dir:
            annotation_path = Path(temporary_dir) / "annotations.json"
            annotation_path.write_text(json.dumps(annotations), encoding="utf-8")
            with self.assertRaises(RoomMapError):
                build_room_maps(
                    ROOT / "static",
                    Path(temporary_dir) / "output",
                    annotations_path=annotation_path,
                )


if __name__ == "__main__":
    unittest.main()
