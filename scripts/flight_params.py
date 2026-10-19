"""Flight search requests ("查10月25日苏黎世到巴黎的机票") -> a Google Flights URL.

Origin, destination and date come from rules only. The page is opened directly
in the Agent browser, so the model only reads results instead of typing into
the search form. Searching needs no account; booking is always the owner's.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date, time
from urllib.parse import quote

from event_params import parse_day

FLIGHT = re.compile(r"机票|航班|飞机票|flights?\b", re.I)
# Common Chinese city names -> the names Google Flights understands best.
CITIES = {
    "苏黎世": "Zurich", "日内瓦": "Geneva", "巴塞尔": "Basel", "巴黎": "Paris", "伦敦": "London",
    "法兰克福": "Frankfurt", "慕尼黑": "Munich", "柏林": "Berlin", "米兰": "Milan", "罗马": "Rome",
    "阿姆斯特丹": "Amsterdam", "维也纳": "Vienna", "马德里": "Madrid", "巴塞罗那": "Barcelona",
    "伊斯坦布尔": "Istanbul", "迪拜": "Dubai", "纽约": "New York", "东京": "Tokyo", "首尔": "Seoul",
    "新加坡": "Singapore", "曼谷": "Bangkok", "香港": "Hong Kong", "北京": "Beijing", "上海": "Shanghai",
    "广州": "Guangzhou", "深圳": "Shenzhen", "成都": "Chengdu", "杭州": "Hangzhou", "西安": "Xi'an",
}
# Main airport per city (IATA), used for Google Flights' structured search link.
AIRPORTS = {
    "Zurich": "ZRH", "Geneva": "GVA", "Basel": "BSL", "Paris": "CDG", "London": "LHR", "Frankfurt": "FRA",
    "Munich": "MUC", "Berlin": "BER", "Milan": "MXP", "Rome": "FCO", "Amsterdam": "AMS", "Vienna": "VIE",
    "Madrid": "MAD", "Barcelona": "BCN", "Istanbul": "IST", "Dubai": "DXB", "New York": "JFK", "Tokyo": "NRT",
    "Seoul": "ICN", "Singapore": "SIN", "Bangkok": "BKK", "Hong Kong": "HKG", "Beijing": "PEK",
    "Shanghai": "PVG", "Guangzhou": "CAN", "Shenzhen": "SZX", "Chengdu": "TFU", "Hangzhou": "HGH", "Xi'an": "XIY",
}
PLACE = r"([一-鿿A-Za-z][一-鿿A-Za-z ]{0,15}?)"


@dataclass(frozen=True)
class FlightQuery:
    origin: str
    destination: str
    day: date

    def url(self) -> str:
        """Results page: a structured link when both airports are known, else a text query."""
        origin, destination = AIRPORTS.get(self.origin), AIRPORTS.get(self.destination)
        if origin and destination:
            return ("https://www.google.com/travel/flights/search?hl=zh-CN&curr=CHF&tfs="
                    + tfs(origin, destination, self.day))
        q = f"Flights from {self.origin} to {self.destination} on {self.day.isoformat()} one way"
        return "https://www.google.com/travel/flights?hl=zh-CN&curr=CHF&q=" + quote(q)


def _field(number: int, value: bytes | int) -> bytes:
    """One protobuf field: varint (int) or length-delimited (bytes)."""
    def varint(n: int) -> bytes:
        out = b""
        while True:
            byte, n = n & 0x7F, n >> 7
            out += bytes([byte | (0x80 if n else 0)])
            if not n:
                return out
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


def tfs(origin: str, destination: str, day: date) -> str:
    """Google Flights' search state: one leg (date, from, to), 1 adult, economy, one way."""
    def airport(code: str) -> bytes:
        return _field(1, 1) + _field(2, code.encode())  # type 1 = airport code
    leg = _field(2, day.isoformat().encode()) + _field(13, airport(origin)) + _field(14, airport(destination))
    # Fields 1/2 open every link Google itself generates; 8/9/19: adults, economy, one way.
    info = _field(1, 28) + _field(2, 2) + _field(3, leg) + _field(8, 1) + _field(9, 1) + _field(19, 2)
    return base64.urlsafe_b64encode(info).decode().rstrip("=")


def _place(text: str) -> str:
    """'明天从苏黎世' -> '苏黎世': a known city at the end wins, else drop date/verb prefixes."""
    text = text.strip()
    for name in sorted(CITIES, key=len, reverse=True):
        if text.endswith(name):
            return name
    return re.sub(r"^(?:.*?\d{1,2}[日号])?\s*(?:今天|明天|后天|大后天)?\s*(?:帮我)?\s*(?:查一?下|看看|查)?\s*(?:从)?",
                  "", text).strip()


def parse_flight(goal: str, today: date) -> FlightQuery | None:
    """A one-way flight search with both cities and one date, or None."""
    if not FLIGHT.search(goal) or re.search(r"往返|返程|回程|round", goal, re.I):
        return None
    day = parse_day(goal, today)
    # Dates are read above; removing them keeps "日" of "20日" out of the city names.
    undated = re.sub(r"(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]", " ", goal)
    route = re.search(r"(?:从)?" + PLACE + r"\s*(?:到|飞|去|至|→|-)\s*" + PLACE + r"\s*(?:的)?(?:机票|航班|飞机票)", undated)
    if day is None or day < today or not route:
        return None
    origin, destination = (_place(part) for part in route.groups())
    if not origin or not destination or origin == destination:
        return None
    return FlightQuery(CITIES.get(origin, origin), CITIES.get(destination, destination), day)


@dataclass(frozen=True)
class Flight:
    price: int  # CHF
    airline: str
    stops: int
    depart: str
    arrive: str
    duration: str
    via: str = ""


CARD = re.compile(
    r"起价：(?P<price>[\d,'’ ]+)\s*瑞士法郎。\s*(?P<airline>.+?)(?:直达航班|航班，中间经停\s*(?P<stops>\d+)\s*站)。"
    r".*?(?P<depart>\d{1,2}:\d{2})\s*离开.+?(?P<arrive>\d{1,2}:\d{2})\s*到达.+?。\s*总时长\s*(?P<duration>[^。]+)。"
    r"(?:\s*在(?P<via>[^的]+?)的)?")


def _duration(text: str) -> str:
    """'1小时\u202f25分钟' / '1 小时 25 分钟' -> '1 小时 25 分钟' (the page mixes space characters)."""
    text = re.sub(r"[\s\u200b-\u200f\u2060\ufeff]+", "", text)
    return re.sub(r"(\d+)(小时|分钟)", r"\1 \2 ", text).strip()


def read_results(texts: list[str]) -> list[Flight]:
    """Flight cards as Google Flights labels them for accessibility (zh-CN, CHF)."""
    flights = []
    for text in texts:
        card = CARD.search(text)
        if card:
            flight = Flight(int(re.sub(r"\D", "", card["price"])), card["airline"].strip(), int(card["stops"] or 0),
                            card["depart"], card["arrive"], _duration(card["duration"]), (card["via"] or "").strip())
            if flight not in flights:
                flights.append(flight)
    return flights


def answer(query: FlightQuery, flights: list[Flight], limit: int = 6) -> str:
    """Chinese summary: cheapest first, direct flights before connections at the same price."""
    chosen = sorted(flights, key=lambda f: (f.price, f.stops, f.depart))[:limit]
    direct = [f for f in flights if f.stops == 0]
    names = {english: chinese for chinese, english in CITIES.items()}
    lines = [f"{query.day.month}月{query.day.day}日 {names.get(query.origin, query.origin)} → "
             f"{names.get(query.destination, query.destination)}，"
             f"共找到 {len(flights)} 个航班（单程、经济舱、1 位成人，价格为 Google 航班显示的起价）："]
    for f in chosen:
        stop = "直达" if f.stops == 0 else f"经停 {f.stops} 次" + (f"（{f.via}）" if f.via else "")
        lines.append(f"· {f.depart}–{f.arrive} {f.airline}，{stop}，{f.duration}，CHF {f.price}")
    if direct:
        cheapest = min(direct, key=lambda f: (f.price, f.depart))
        lines.append(f"最便宜的直达：{cheapest.airline} {cheapest.depart}–{cheapest.arrive}，CHF {cheapest.price}。")
    lines.append("这里只是查询结果，没有选择或预订任何航班。")
    # One-tap follow-ups: put a chosen flight into the calendar (an ordinary create-event request).
    for f in calendar_choices(chosen):
        lines.append(f"[[action:加日历 {f.airline} {f.depart}|{calendar_request(query, f, names)}]]")
    return "\n".join(lines)


def calendar_choices(flights: list[Flight], limit: int = 3) -> list[Flight]:
    """Direct flights first, landing the same day (a calendar event needs end after start)."""
    same_day = [f for f in flights if f.arrive > f.depart]
    return sorted(same_day, key=lambda f: (f.stops, f.price, f.depart))[:limit]


def calendar_request(query: FlightQuery, flight: Flight, names: dict[str, str]) -> str:
    from itinerary import Proposal
    hour, minute = map(int, flight.depart.split(":"))
    end_hour, end_minute = map(int, flight.arrive.split(":"))
    title = (f"{flight.airline} {names.get(query.origin, query.origin)}→"
             f"{names.get(query.destination, query.destination)}")
    return Proposal(title, query.day, time(hour, minute), time(end_hour, end_minute)).request()
