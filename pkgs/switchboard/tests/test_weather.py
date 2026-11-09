from switchboard import intents, sources


P = [
    sources.Period("This Afternoon", "2026-09-13", True, 87, "Mostly Sunny", 12),
    sources.Period("Tonight", "2026-09-13", False, 61, "Slight Chance Showers And Thunderstorms then Partly Cloudy", 20),
    sources.Period("Monday", "2026-09-14", True, 79, "Sunny", 0),
    sources.Period("Monday Night", "2026-09-14", False, 60, "Mostly Clear", None),
    sources.Period("Tuesday", "2026-09-15", True, 91, "Sunny", 2),
]


def test_scope_routing() -> None:
    assert intents.route("what's the weather tomorrow") == "weather:tomorrow"
    assert intents.route("is it going to rain tomorrow?") == "weather:tomorrow"
    assert intents.route("what's the weather today") == "weather:today"
    assert intents.route("weather tonight") == "weather:today"
    assert intents.route("what's the forecast") == "weather:both"
    assert intents.route("check the weather") == "weather:both"
    assert intents.route("what did ryan hall say") == "standing:ryan-hall"   # not the weather intent


def test_period_selection_by_scope() -> None:
    today, tomorrow = "2026-09-13", "2026-09-14"
    t = intents._weather_text(P, "today", today, tomorrow)
    assert t.startswith("This Afternoon: mostly sunny, high near 87, 12 percent chance of rain. Tonight:")
    assert "Monday" not in t
    t = intents._weather_text(P, "tomorrow", today, tomorrow)
    assert t == "Monday: sunny, high near 79. Monday Night: mostly clear, low around 60."
    t = intents._weather_text(P, "both", today, tomorrow)
    assert t.count(":") == 4 and "Tuesday" not in t
    assert intents._weather_text(P, "tomorrow", "2026-09-20", "2026-09-21") == "I don't have a forecast for that day yet."


def test_period_sentence_phrasing() -> None:
    assert intents._period_sentence(P[1]) == (
        "Tonight: slight chance of showers and thunderstorms then partly cloudy, low around 61, 20 percent chance of rain.")
    assert intents._period_sentence(P[3]) == "Monday Night: mostly clear, low around 60."
