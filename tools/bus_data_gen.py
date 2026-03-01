from pathlib import Path

import jsonpickle


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


rsA = RouteScheduleP(
    1,
    Route(1, [east, north, west]),
    [
        (["07:30", None, "07:40"], True),
        (["09:20", None, "09:30"], False),
        (["09:35", None, "09:45"], False),
        (["11:35", None, "11:45"], True),
        (["12:15", None, "12:25"], False),
        (["13:30", None, "13:40"], True),
        (["15:30", None, "15:40"], False),
        (["15:50", None, "16:00"], False),
        (["17:30", None, "17:40"], True),
        (["17:50", None, "18:00"], False),
        (["18:40", None, "18:50"], True),
        (["20:10", None, "20:20"], False),
        (["21:15", None, "21:25"], True),
        (["22:10", None, "22:20"], False),
    ],
)

rsB = RouteScheduleP(
    2,
    Route(2, [west, north, east]),
    [
        (["07:40", None, "07:50"], True),
        (["09:30", None, "09:40"], False),
        (["09:45", None, "09:55"], False),
        (["11:45", None, "11:55"], True),
        (["12:25", None, "12:35"], False),
        (["13:40", None, "13:50"], True),
        (["15:40", None, "15:50"], False),
        (["16:00", None, "16:10"], False),
        (["17:40", None, "17:50"], True),
        (["18:00", None, "18:10"], False),
        (["18:50", None, "19:00"], True),
        (["20:20", None, "20:30"], False),
        (["21:25", None, "21:35"], True),
        (["22:20", None, "22:30"], False),
    ],
)

rsC = RouteScheduleP(
    3,
    Route(3, [east, south]),
    [
        (["07:30", "07:45"], True),
        (["08:30", "08:45"], False),
        (["11:35", "11:50"], False),
        (["11:45", "12:00"], True),
        (["12:10", "12:25"], False),
        (["12:35", "12:50"], False),
        (["14:30", "14:45"], False),
        (["17:25", "17:40"], False),
        (["17:45", "18:00"], True),
        (["18:10", "18:25"], False),
        (["19:00", "19:15"], True),
        (["20:30", "20:45"], False),
        (["21:35", "21:50"], True),
        (["22:30", "22:45"], False),
    ],
)

rsD = RouteScheduleP(
    4,
    Route(4, [south, east]),
    [
        (["07:10", "07:25"], False),
        (["07:30", "07:45"], True),
        (["08:00", "08:15"], False),
        (["08:30", "08:45"], False),
        (["09:00", "09:15"], False),
        (["12:05", "12:20"], False),
        (["13:20", "13:35"], False),
        (["13:40", "13:55"], True),
        (["14:00", "14:15"], False),
        (["15:10", "15:25"], False),
        (["18:20", "18:35"], False),
        (["19:15", "19:30"], True),
        (["20:45", "21:00"], False),
        (["21:50", "22:05"], True),
        (["22:45", "23:00"], False),
    ],
)

rsE = RouteScheduleP(
    5,
    Route(5, [west, south]),
    [
        (["07:35", "07:55"], False),
        (["11:35", "11:55"], True),
        (["12:25", "12:45"], True),
        (["17:35", "17:55"], True),
        (["18:00", "18:20"], False),
        (["18:50", "19:10"], True),
        (["20:20", "20:40"], False),
        (["21:25", "21:45"], True),
        (["22:20", "22:40"], False),
    ],
)

rsF = RouteScheduleP(
    6,
    Route(6, [south, west]),
    [
        (["07:10", "07:30"], False),
        (["07:30", "07:50"], True),
        (["08:00", "08:20"], False),
        (["08:30", "08:50"], False),
        (["09:00", "09:20"], False),
        (["13:20", "13:40"], False),
        (["13:40", "14:00"], True),
        (["14:00", "14:20"], False),
        (["15:10", "15:30"], False),
    ],
)

rsG = RouteSchedule(
    7,
    Route(7, [gaoxin, xianyanyuan, west, east]),
    [
        ["06:40", "06:45", None, "07:25"],
        ["08:00", "08:05", None, "08:50"],
        ["09:35", "09:40", None, "10:20"],
        ["12:50", "12:55", None, "13:35"],
        ["14:30", "14:35", None, "15:25"],
        ["16:00", "16:05", None, "16:50"],
        ["18:30", "18:35", None, "19:25"],
        ["22:05", "22:10", None, "22:50"],
    ],
)

rsGweekend = RouteSchedule(
    9,
    Route(7, [gaoxin, xianyanyuan, west, east]),
    [
        ["08:00", "08:05", None, "08:50"],
        ["13:40", "13:45", None, "14:30"],
        ["16:00", "16:05", None, "16:50"],
        ["21:50", "21:55", None, "22:40"],
    ],
)

rsH = RouteSchedule(
    8,
    Route(8, [east, west, xianyanyuan, gaoxin]),
    [
        ["06:50", "07:00", None, "07:40"],
        ["08:00", "08:10", None, "09:00"],
        ["12:50", "13:00", None, "13:40"],
        ["14:30", "14:40", None, "15:25"],
        ["16:00", "16:10", None, "16:50"],
        ["18:30", "18:40", None, "19:30"],
        ["21:20", "21:30", None, "22:00"],
        ["22:05", "22:15", None, "23:00"],
    ],
)

rsHweekend = RouteSchedule(
    10,
    Route(8, [east, west, xianyanyuan, gaoxin]),
    [
        ["07:00", "07:10", None, "07:50"],
        ["12:50", "13:00", None, "13:40"],
        ["18:30", "18:40", None, "19:30"],
        ["21:50", "22:00", None, "22:50"],
    ],
)

rsI = RouteSchedule(
    11,
    Route(11, [gaoxin, xianyanyuan]),
    [
        ["07:20", "07:25"],
        ["10:40", "10:45"],
        ["13:30", "13:35"],
        ["17:45", "17:50"],
    ],
)

rsJ = RouteSchedule(
    12,
    Route(12, [xianyanyuan, gaoxin]),
    [
        ["07:25", "07:35"],
        ["10:45", "10:55"],
        ["13:35", "13:45"],
        ["17:50", "18:00"],
    ],
)


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
    weekday_routes=list(map(lambda x: x.convert(False), [rsA, rsB, rsC, rsD, rsE, rsF]))
    + [rsG, rsH, rsI, rsJ],
    weekend_routes=list(map(lambda x: x.convert(True), [rsA, rsB, rsC, rsD, rsE, rsF]))
    + [rsGweekend, rsHweekend],
    message=Message(
        message="本表为 2026 春季学期时间表，来源：蜗壳小道消息",
        url="https://mp.weixin.qq.com/s/aWF0UA63pQmM5MWiAtTeKg",
    ),
)


def generate_bus_data(output_path: Path | None = None) -> Path:
    data_json = jsonpickle.encode(data, unpicklable=False)
    if output_path is None:
        output_path = (
            Path(__file__).resolve().parent.parent / "static" / "bus_data_v3.json"
        )
    with open(output_path, "w") as f:
        f.write(str(data_json))
    return output_path


if __name__ == "__main__":
    generate_bus_data()
