import base64
import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from agent_loop import AndroidController
from flight_params import FlightQuery, parse_flight

TODAY = date(2026, 10, 16)


class FlightParamsTest(unittest.TestCase):
    def test_route_and_date_come_from_the_request(self):
        cases = {"查10月25日苏黎世到巴黎的机票": FlightQuery("Zurich", "Paris", date(2026, 10, 25)),
                 "帮我看看10月30日从上海飞北京的航班": FlightQuery("Shanghai", "Beijing", date(2026, 10, 30)),
                 "明天苏黎世到伦敦的机票": FlightQuery("Zurich", "London", date(2026, 10, 17)),
                 "10月20日天津到日照的机票": FlightQuery("天津", "日照", date(2026, 10, 20))}
        for goal, expected in cases.items():
            with self.subTest(goal=goal):
                self.assertEqual(expected, parse_flight(goal, TODAY))
        for goal in ("苏黎世到巴黎的机票", "查10月25日苏黎世到巴黎往返机票", "10月25日去巴黎"):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_flight(goal, TODAY))

    def test_structured_link_names_both_airports_date_and_one_way(self):
        url = parse_flight("查10月25日苏黎世到巴黎的机票", TODAY).url()
        tfs = url.split("tfs=")[1]
        raw = base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))
        for part in (b"2026-10-25", b"ZRH", b"CDG", b"\x98\x01\x02"):  # field 19 = 2: one way
            self.assertIn(part, raw)
        self.assertIn("text query", FlightQuery.url.__doc__ or "")  # unknown airports fall back to q=
        self.assertIn("q=", FlightQuery("天津", "日照", date(2026, 10, 20)).url())

    def test_result_cards_are_read_and_summarised_without_the_model(self):
        from flight_params import answer, read_results
        cards = ["起价：166 瑞士法郎。 法航直达航班。 星期日, 十月 25 10:45 离开苏黎世机场，星期日, 十月 25 12:10 "
                 "到达巴黎夏尔·戴高乐机场。 总时长 1小时\u202f25分钟。 二氧化碳估算排放量：67 千克. 选择航班",
                 "起价：122 瑞士法郎。 神鹰航空航班，中间经停 1 站。 星期日, 十月 25 16:50 离开苏黎世机场，星期日, 十月 25 21:30 "
                 "到达巴黎夏尔·戴高乐机场。 总时长 4 小时 40 分钟。 在美因河畔法兰克福的法兰克福机场用时 2 小时 25 分钟的转机。",
                 "跟踪苏黎世至巴黎的机票价格，2026-10-25出发"]
        flights = read_results(cards + cards[:1])  # a card read twice counts once
        self.assertEqual([(166, "法航", 0, "10:45", "12:10", "1 小时 25 分钟"), (122, "神鹰航空", 1, "16:50", "21:30", "4 小时 40 分钟")],
                         [(f.price, f.airline, f.stops, f.depart, f.arrive, f.duration) for f in flights])
        text = answer(FlightQuery("Zurich", "Paris", date(2026, 10, 25)), flights)
        self.assertIn("10月25日 苏黎世 → 巴黎，共找到 2 个航班", text)
        self.assertLess(text.index("CHF 122"), text.index("CHF 166"))  # cheapest first
        self.assertIn("最便宜的直达：法航 10:45–12:10，CHF 166", text)
        self.assertIn("没有选择或预订任何航班", text)
        # The direct flight gets a one-tap calendar request the create-event rules understand.
        from event_params import resolve
        button = next(line for line in text.splitlines() if line.startswith("[[action:加日历 法航 10:45|"))
        params = resolve(button.split("|", 1)[1].rstrip("]"), "", date(2026, 10, 16))
        self.assertEqual(("法航 苏黎世→巴黎", date(2026, 10, 25), time(10, 45), time(12, 10)),
                         (params.title, params.day, params.start, params.end))

    def test_open_url_is_quoted_for_the_device_shell(self):
        controller = AndroidController.__new__(AndroidController)
        controller.dry_run, controller.sms_task = False, False
        controller.require_display = Mock(return_value=2)
        controller.require_isolation = Mock()
        controller.require_owner_focus = Mock(return_value={})
        controller._run = Mock(return_value="")
        controller.verify_package_on_agent_display = Mock()
        controller.execute({"action": "open_url", "arguments": {"url": "https://a.example/s?x=1&y=2"}})
        command = next(c.args for c in controller._run.call_args_list if "start" in c.args)
        self.assertEqual("'https://a.example/s?x=1&y=2'", command[command.index("-d") + 1])


if __name__ == "__main__":
    unittest.main()
