import json
from pathlib import Path


class Campus:
    def __init__(self, id: int, name: str, latitude: float, longitude: float):
        self.id = id
        self.name = name
        self.latitude = latitude
        self.longitude = longitude

    id: int
    name: str
    latitude: float
    longitude: float


east = Campus(1, "东区", 117.268264, 31.83892)
west = Campus(2, "西区", 117.256645, 31.839258)
north = Campus(3, "北区", 117.268125, 31.841933)
south = Campus(4, "南区", 117.283853, 31.822112)
xianyanyuan = Campus(5, "先研院", 117.129257, 31.826345)
gaoxin = Campus(6, "高新", 117.129369, 31.820447)


class Route:
    def __init__(self, id: int, campuses: list[Campus]):
        self.id = id
        self.campuses = campuses

    id: int
    campuses: list[Campus]


class RouteSchedule:
    def __init__(self, id: int, route: Route, time: list[list[str | None]]):
        self.id = id
        self.route = route
        self.time = time

    id: int
    route: Route
    time: list[list[str | None]]


class RouteScheduleP:
    def __init__(
        self, id: int, route: Route, time: list[tuple[list[str | None], bool]]
    ):
        self.id = id
        self.route = route
        self.time = time

    id: int
    route: Route
    time: list[tuple[list[str | None], bool]]

    def convert(self, is_weekend: bool) -> RouteSchedule:
        if is_weekend:
            return RouteSchedule(self.id, self.route, [x[0] for x in self.time if x[1]])
        else:
            return RouteSchedule(self.id, self.route, [x[0] for x in self.time])


# 2026 暑期时刻表（8月1日—8月29日），工作日与周末相同
rsA = RouteSchedule(
    1,
    Route(1, [east, north, west]),
    [
        ["08:15", None, "08:25"],
        ["11:30", None, "11:40"],
        ["14:10", None, "14:20"],
        ["17:00", None, "17:10"],
        ["19:00", None, "19:10"],
    ],
)

rsB = RouteSchedule(
    2,
    Route(2, [west, north, east]),
    [
        ["08:25", None, "08:35"],
        ["11:40", None, "11:50"],
        ["14:20", None, "14:30"],
        ["17:10", None, "17:20"],
        ["19:10", None, "19:20"],
    ],
)

rsC = RouteSchedule(
    3,
    Route(3, [east, south]),
    [
        ["11:40", "11:55"],
        ["17:10", "17:25"],
        ["19:20", "19:35"],
    ],
)

rsD = RouteSchedule(
    4,
    Route(4, [south, east]),
    [
        ["08:00", "08:15"],
        ["14:10", "14:25"],
        ["19:35", "19:50"],
    ],
)

rsE = RouteSchedule(
    5,
    Route(5, [west, south]),
    [
        ["11:30", "11:50"],
        ["17:00", "17:20"],
        ["19:10", "19:30"],
    ],
)

rsF = RouteSchedule(
    6,
    Route(6, [south, west]),
    [
        ["08:00", "08:20"],
        ["14:10", "14:30"],
    ],
)

rsG = RouteSchedule(
    7,
    Route(7, [gaoxin, xianyanyuan, west, east]),
    [
        ["08:45", "08:50", None, "09:35"],
        ["13:30", "13:35", None, "14:20"],
        ["19:00", "19:05", None, "19:50"],
        ["21:00", "21:05", None, "21:50"],
    ],
)

rsH = RouteSchedule(
    8,
    Route(8, [east, west, xianyanyuan, gaoxin]),
    [
        ["07:30", "07:40", None, "08:20"],
        ["12:30", "12:40", None, "13:20"],
        ["18:00", "18:10", None, "18:50"],
        ["20:00", "20:10", None, "20:50"],
    ],
)

summer_routes = [rsA, rsB, rsC, rsD, rsE, rsF, rsG, rsH]


class Message:
    def __init__(self, message: str, url: str):
        self.message = message
        self.url = url

    message: str
    url: str


class BusData:
    def __init__(
        self,
        campuses: list[Campus],
        routes: list[Route],
        weekday_routes: list[RouteSchedule],
        weekend_routes: list[RouteSchedule],
        message: Message,
    ):
        self.campuses = campuses
        self.routes = routes
        self.weekday_routes = weekday_routes
        self.weekend_routes = weekend_routes
        self.message = message

    campuses: list[Campus]
    routes: list[Route]
    weekday_routes: list[RouteSchedule]
    weekend_routes: list[RouteSchedule]
    message: Message


data = BusData(
    campuses=[east, west, north, south, xianyanyuan, gaoxin],
    routes=[
        Route(1, [east, north, west]),
        Route(2, [west, north, east]),
        Route(3, [east, south]),
        Route(4, [south, east]),
        Route(5, [west, south]),
        Route(6, [south, west]),
        Route(7, [gaoxin, xianyanyuan, west, east]),
        Route(8, [east, west, xianyanyuan, gaoxin]),
        Route(11, [gaoxin, xianyanyuan]),
        Route(12, [xianyanyuan, gaoxin]),
    ],
    weekday_routes=summer_routes,
    weekend_routes=summer_routes,
    message=Message(
        message="本表为 2026 暑期时间表（8月1日—8月29日），来源：蜗壳小道消息",
        url="https://mp.weixin.qq.com/s/aWF0UA63pQmM5MWiAtTeKg",
    ),
)


def generate_bus_data(output_path: Path | None = None) -> Path:
    data_json = json.dumps(
        data,
        default=lambda value: value.__dict__,
        ensure_ascii=False,
    )
    if output_path is None:
        output_path = (
            Path(__file__).resolve().parent.parent / "static" / "bus_data_v3.json"
        )
    with open(output_path, "w") as f:
        f.write(str(data_json))
    return output_path


if __name__ == "__main__":
    generate_bus_data()
