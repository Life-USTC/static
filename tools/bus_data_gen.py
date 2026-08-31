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


east = Campus(1, "东区", 31.83892, 117.268264)
west = Campus(2, "西区", 31.839258, 117.256645)
north = Campus(3, "北区", 31.841933, 117.268125)
south = Campus(4, "南区", 31.822112, 117.283853)
xianyanyuan = Campus(5, "先研院", 31.826345, 117.129257)
gaoxin = Campus(6, "高新", 31.820447, 117.129369)
taihu = Campus(7, "太湖路园区", 31.824495, 117.296554)


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


east_west = Route(1, [east, north, west])
west_east = Route(2, [west, north, east])
east_south = Route(3, [east, south])
south_east = Route(4, [south, east])
west_south = Route(5, [west, north, south])
south_west = Route(6, [south, north, west])
gaoxin_east = Route(7, [gaoxin, xianyanyuan, west, east])
east_gaoxin = Route(8, [east, west, xianyanyuan, gaoxin])
taihu_east = Route(13, [taihu, east])
east_taihu = Route(14, [east, taihu])


# 校园班车运行时刻表（2026 年 8 月 30 日试运行）。
# 工作日表包含图中所有工作日班次；双休表仅包含红色 ★ 班次。
campus_weekday_routes = [
    RouteSchedule(
        1,
        east_west,
        [
            ["07:30", None, "07:40"],
            ["09:20", None, "09:30"],
            ["09:35", None, "09:45"],
            ["11:35", None, "11:45"],
            ["12:15", None, "12:25"],
            ["13:30", None, "13:40"],
            ["15:30", None, "15:40"],
            ["15:50", None, "16:00"],
            ["17:30", None, "17:40"],
            ["17:50", None, "18:00"],
            ["18:40", None, "18:50"],
            ["20:10", None, "20:20"],
            ["21:15", None, "21:25"],
            ["22:10", None, "22:20"],
        ],
    ),
    RouteSchedule(
        2,
        west_east,
        [
            ["07:40", None, "07:50"],
            ["09:30", None, "09:40"],
            ["09:45", None, "09:55"],
            ["11:45", None, "11:55"],
            ["12:25", None, "12:35"],
            ["13:40", None, "13:50"],
            ["15:40", None, "15:50"],
            ["16:00", None, "16:10"],
            ["17:40", None, "17:50"],
            ["18:00", None, "18:10"],
            ["18:50", None, "19:00"],
            ["20:20", None, "20:30"],
            ["21:25", None, "21:35"],
            ["22:20", None, "22:30"],
        ],
    ),
    RouteSchedule(
        3,
        east_south,
        [
            ["07:30", "07:45"],
            ["08:30", "08:45"],
            ["11:35", "11:50"],
            ["11:45", "12:00"],
            ["12:10", "12:25"],
            ["12:35", "12:50"],
            ["14:30", "14:45"],
            ["17:25", "17:40"],
            ["17:45", "18:00"],
            ["18:10", "18:25"],
            ["19:00", "19:15"],
            ["20:30", "20:45"],
            ["21:35", "21:50"],
            ["22:30", "22:45"],
        ],
    ),
    RouteSchedule(
        4,
        south_east,
        [
            ["07:10", "07:25"],
            ["07:30", "07:45"],
            ["08:00", "08:15"],
            ["08:30", "08:45"],
            ["09:00", "09:15"],
            ["12:05", "12:20"],
            ["13:20", "13:35"],
            ["13:40", "13:55"],
            ["14:00", "14:15"],
            ["15:10", "15:25"],
            ["18:20", "18:35"],
            ["19:15", "19:30"],
            ["20:45", "21:00"],
            ["21:50", "22:05"],
            ["22:45", "23:00"],
        ],
    ),
    RouteSchedule(
        5,
        west_south,
        [
            ["07:35", None, "07:55"],
            ["11:35", None, "11:55"],
            ["12:25", None, "12:45"],
            ["17:35", None, "17:55"],
            ["18:00", None, "18:20"],
            ["18:50", None, "19:10"],
            ["20:20", None, "20:40"],
            ["21:25", None, "21:45"],
            ["22:20", None, "22:40"],
        ],
    ),
    RouteSchedule(
        6,
        south_west,
        [
            ["07:10", None, "07:30"],
            ["07:30", None, "07:50"],
            ["08:00", None, "08:20"],
            ["08:30", None, "08:50"],
            ["09:00", None, "09:20"],
            ["13:20", None, "13:40"],
            ["13:40", None, "14:00"],
            ["14:00", None, "14:20"],
            ["15:10", None, "15:30"],
        ],
    ),
]

campus_saturday_routes = [
    RouteSchedule(
        1,
        east_west,
        [
            ["07:30", None, "07:40"],
            ["11:35", None, "11:45"],
            ["13:30", None, "13:40"],
            ["17:30", None, "17:40"],
            ["18:40", None, "18:50"],
            ["21:15", None, "21:25"],
        ],
    ),
    RouteSchedule(
        2,
        west_east,
        [
            ["07:40", None, "07:50"],
            ["11:45", None, "11:55"],
            ["13:40", None, "13:50"],
            ["17:40", None, "17:50"],
            ["18:50", None, "19:00"],
            ["21:25", None, "21:35"],
        ],
    ),
    RouteSchedule(
        3,
        east_south,
        [
            ["11:45", "12:00"],
            ["17:45", "18:00"],
            ["19:00", "19:15"],
            ["21:35", "21:50"],
        ],
    ),
    RouteSchedule(
        4,
        south_east,
        [
            ["07:30", "07:45"],
            ["13:40", "13:55"],
            ["19:15", "19:30"],
            ["21:50", "22:05"],
        ],
    ),
    RouteSchedule(
        5,
        west_south,
        [
            ["11:35", None, "11:55"],
            ["17:35", None, "17:55"],
            ["18:50", None, "19:10"],
            ["21:25", None, "21:45"],
        ],
    ),
    RouteSchedule(
        6,
        south_west,
        [["07:30", None, "07:50"], ["13:40", None, "14:00"]],
    ),
]


# 高新校区班车运行时刻表（2026 年 8 月 30 日试运行）。
gaoxin_weekday_routes = [
    RouteSchedule(
        7,
        gaoxin_east,
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
    ),
    RouteSchedule(
        8,
        east_gaoxin,
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
    ),
]

gaoxin_saturday_routes = [
    RouteSchedule(
        9,
        gaoxin_east,
        [
            ["08:00", "08:05", None, "08:50"],
            ["13:40", "13:45", None, "14:30"],
            ["16:00", "16:05", None, "16:50"],
            ["21:50", "21:55", None, "22:40"],
        ],
    ),
    RouteSchedule(
        10,
        east_gaoxin,
        [
            ["06:50", "07:00", None, "07:40"],
            ["12:50", "13:00", None, "13:40"],
            ["18:30", "18:40", None, "19:30"],
            ["21:50", "22:00", None, "22:50"],
        ],
    ),
]


# 太湖路园区班车运行时刻表（2026 年 8 月 27 日试运行）。
taihu_weekday_routes = [
    RouteSchedule(
        13,
        taihu_east,
        [
            ["09:10", "09:40"],
            ["10:30", "11:00"],
            ["11:30", "12:00"],
            ["13:30", "13:50"],
            ["15:00", "15:30"],
            ["18:30", "19:00"],
            ["21:30", "22:00"],
        ],
    ),
    RouteSchedule(
        14,
        east_taihu,
        [
            ["07:10", "07:40"],
            ["09:10", "09:40"],
            ["10:30", "11:00"],
            ["11:30", "12:00"],
            ["13:30", "13:50"],
            ["18:40", "19:10"],
            ["22:10", "22:40"],
        ],
    ),
]

taihu_saturday_routes = [
    RouteSchedule(
        13,
        taihu_east,
        [
            ["07:10", "07:40"],
            ["09:10", "09:40"],
            ["10:30", "11:00"],
            ["11:30", "12:00"],
            ["13:30", "13:50"],
            ["15:00", "15:30"],
            ["18:30", "19:00"],
            ["21:30", "22:00"],
        ],
    ),
    RouteSchedule(
        14,
        east_taihu,
        [
            ["07:10", "07:40"],
            ["09:10", "09:40"],
            ["10:30", "11:00"],
            ["11:30", "12:00"],
            ["13:30", "13:50"],
            ["18:40", "19:10"],
            ["22:10", "22:40"],
        ],
    ),
]

taihu_sunday_routes = [
    RouteSchedule(
        13,
        taihu_east,
        [["11:30", "12:00"], ["13:30", "13:50"], ["21:30", "22:00"]],
    ),
    RouteSchedule(
        14,
        east_taihu,
        [
            ["07:30", "08:00"],
            ["11:30", "12:00"],
            ["13:30", "13:50"],
            ["22:10", "22:40"],
        ],
    ),
]


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
        saturday_routes: list[RouteSchedule],
        sunday_routes: list[RouteSchedule],
        message: Message,
    ):
        self.campuses = campuses
        self.routes = routes
        self.weekday_routes = weekday_routes
        self.saturday_routes = saturday_routes
        self.sunday_routes = sunday_routes
        self.message = message

    campuses: list[Campus]
    routes: list[Route]
    weekday_routes: list[RouteSchedule]
    saturday_routes: list[RouteSchedule]
    sunday_routes: list[RouteSchedule]
    message: Message


data = BusData(
    campuses=[east, west, north, south, xianyanyuan, gaoxin, taihu],
    routes=[
        east_west,
        west_east,
        east_south,
        south_east,
        west_south,
        south_west,
        gaoxin_east,
        east_gaoxin,
        taihu_east,
        east_taihu,
    ],
    weekday_routes=campus_weekday_routes + gaoxin_weekday_routes + taihu_weekday_routes,
    saturday_routes=campus_saturday_routes
    + gaoxin_saturday_routes
    + taihu_saturday_routes,
    sunday_routes=campus_saturday_routes + gaoxin_saturday_routes + taihu_sunday_routes,
    message=Message(
        message=(
            "校园及高新校区班车自2026年8月30日起试运行，"
            "太湖路园区班车自2026年8月27日起试运行。来源：中国科大官方时刻表。"
        ),
        url="https://www.ustc.edu.cn/ggfw/rdlj.htm",
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
    with open(output_path, "w", encoding="utf-8") as output_file:
        output_file.write(data_json)
    return output_path


if __name__ == "__main__":
    generate_bus_data()
