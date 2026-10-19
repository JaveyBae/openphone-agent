import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_loop import explicit_target_package  # noqa: E402
from app_targets import RAILWAY_PACKAGE  # noqa: E402


class RailwayRoutingTest(unittest.TestCase):
    def test_train_ticket_goals_open_12306(self):
        for goal in ("查询上海到北京最近三天最便宜的高铁票", "用12306查10月25日北京到上海的车次",
                     "看看明天去杭州的动车票还有没有", "查一下周五的火车票"):
            self.assertEqual(RAILWAY_PACKAGE, explicit_target_package(goal), goal)

    def test_other_goals_are_not_routed_to_12306(self):
        for goal in ("查10月25日苏黎世到巴黎的机票", "在 Google 地图里查去火车站的路线"):
            self.assertNotEqual(RAILWAY_PACKAGE, explicit_target_package(goal), goal)


if __name__ == "__main__":
    unittest.main()
