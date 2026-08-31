import json
import tempfile
import unittest
from pathlib import Path

from tools.bus_data_gen import generate_bus_data

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "static" / "bus_data_v3.json"
DAY_TYPES = ("weekday_routes", "saturday_routes", "sunday_routes")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_schedule(payload: dict, day_type: str, route_id: int) -> dict:
    return next(
        schedule
        for schedule in payload[day_type]
        if schedule["route"]["id"] == route_id
    )


class BusDataGeneratorTest(unittest.TestCase):
    def test_generated_artifact_matches_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            generated_path = generate_bus_data(Path(temporary_dir) / "bus.json")
            self.assertEqual(load_json(generated_path), load_json(ARTIFACT))

    def test_campus_and_route_ids_are_unique(self) -> None:
        payload = load_json(ARTIFACT)
        campus_ids = [campus["id"] for campus in payload["campuses"]]
        route_ids = [route["id"] for route in payload["routes"]]
        self.assertEqual(len(campus_ids), len(set(campus_ids)))
        self.assertEqual(len(route_ids), len(set(route_ids)))
        routes_by_id = {route["id"]: route for route in payload["routes"]}
        self.assertEqual(
            [campus["id"] for campus in routes_by_id[5]["campuses"]],
            [2, 3, 4],
        )
        self.assertEqual(
            [campus["id"] for campus in routes_by_id[6]["campuses"]],
            [4, 3, 2],
        )
        self.assertEqual(
            payload["campuses"][-1],
            {
                "id": 7,
                "name": "太湖路园区",
                "latitude": 31.824495,
                "longitude": 117.296554,
            },
        )

    def test_every_stop_time_row_matches_route_stop_count(self) -> None:
        payload = load_json(ARTIFACT)
        for day_type in DAY_TYPES:
            for schedule in payload[day_type]:
                stop_count = len(schedule["route"]["campuses"])
                self.assertTrue(schedule["time"])
                for stop_times in schedule["time"]:
                    self.assertEqual(len(stop_times), stop_count)

    def test_taihu_has_day_specific_departures(self) -> None:
        payload = load_json(ARTIFACT)
        weekday_taihu_to_east = find_schedule(payload, "weekday_routes", 13)
        saturday_taihu_to_east = find_schedule(payload, "saturday_routes", 13)
        sunday_taihu_to_east = find_schedule(payload, "sunday_routes", 13)

        self.assertNotIn(["07:10", "07:40"], weekday_taihu_to_east["time"])
        self.assertEqual(saturday_taihu_to_east["time"][0], ["07:10", "07:40"])
        self.assertEqual(
            sunday_taihu_to_east["time"],
            [["11:30", "12:00"], ["13:30", "13:50"], ["21:30", "22:00"]],
        )

        weekday_east_to_taihu = find_schedule(payload, "weekday_routes", 14)
        sunday_east_to_taihu = find_schedule(payload, "sunday_routes", 14)
        self.assertEqual(weekday_east_to_taihu["time"][0], ["07:10", "07:40"])
        self.assertEqual(sunday_east_to_taihu["time"][0], ["07:30", "08:00"])

    def test_schema_and_message_use_current_sources(self) -> None:
        payload = load_json(ARTIFACT)
        self.assertNotIn("weekend_routes", payload)
        message = payload["message"]
        self.assertIn("2026年8月30日", message["message"])
        self.assertIn("2026年8月27日", message["message"])
        self.assertEqual(message["url"], "https://www.ustc.edu.cn/ggfw/rdlj.htm")


if __name__ == "__main__":
    unittest.main()
