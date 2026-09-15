"""Recipes by phone — Tandoor's REST API on the fast path.

  "do I have a recipe for chili" / "something with eggplant"
      -> search (name, keywords, ingredients); the closest few, numbered
  "ingredients for <name>" / "ingredients" (for the last one found)
      -> the ingredient list, served as a list
  "steps" / "next step"     -> one step at a time, like the newsletter's "next"
  "number 2"                -> pick from the last search

Whisper mangles recipe names, so search is fuzzy by design and the phone
offers the closest few rather than pretending it heard one exactly. The
token is read-scoped; nothing here writes to Tandoor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from .config import Settings
from .sources import SourceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    id: int
    name: str
    by_ingredient: bool = False   # matched via the food index, not the name


# Unit abbreviations as Tandoor stores them -> (singular, plural). Kokoro
# spells "lb" and "tsp" letter by letter otherwise (Chris, 2026-09-15).
UNITS: dict[str, tuple[str, str]] = {
    "lb": ("pound", "pounds"), "lbs": ("pound", "pounds"), "pound": ("pound", "pounds"),
    "oz": ("ounce", "ounces"), "fl oz": ("fluid ounce", "fluid ounces"),
    "tsp": ("teaspoon", "teaspoons"), "tbsp": ("tablespoon", "tablespoons"), "tbs": ("tablespoon", "tablespoons"), "tbl": ("tablespoon", "tablespoons"),
    "c": ("cup", "cups"), "cup": ("cup", "cups"), "qt": ("quart", "quarts"), "pt": ("pint", "pints"), "gal": ("gallon", "gallons"),
    "g": ("gram", "grams"), "kg": ("kilogram", "kilograms"), "ml": ("milliliter", "milliliters"), "l": ("liter", "liters"),
    "pkg": ("package", "packages"), "pkt": ("packet", "packets"), "env": ("envelope", "envelopes"),
    "can": ("can", "cans"), "jar": ("jar", "jars"), "bag": ("bag", "bags"), "box": ("box", "boxes"), "bottle": ("bottle", "bottles"),
    "bunch": ("bunch", "bunches"), "head": ("head", "heads"), "clove": ("clove", "cloves"), "stick": ("stick", "sticks"),
    "slice": ("slice", "slices"), "piece": ("piece", "pieces"), "pinch": ("pinch", "pinches"), "dash": ("dash", "dashes"),
    "sprig": ("sprig", "sprigs"), "stalk": ("stalk", "stalks"), "ear": ("ear", "ears"), "sheet": ("sheet", "sheets"),
}
# Units that read as "<n> <unit> of <food>" — the "of" is what makes "one can
# of white beans" a noun phrase instead of "I can".
_OF_UNITS = {"can", "jar", "bag", "box", "bottle", "bunch", "head", "clove", "stick", "slice", "piece", "pinch", "dash",
             "sprig", "stalk", "ear", "sheet", "package", "packet", "envelope", "pound", "ounce", "fluid ounce", "cup",
             "quart", "pint", "gallon", "gram", "kilogram", "milliliter", "liter", "teaspoon", "tablespoon"}
# Word-level abbreviations inside food names and notes.
_ABBR = {"lg": "large", "sm": "small", "med": "medium", "approx": "about", "w/": "with", "w/o": "without",
         "pkg": "package", "tbsp": "tablespoon", "tsp": "teaspoon", "oz": "ounce", "lb": "pound", "lbs": "pounds", "qt": "quart"}


def _unit_words(unit: str, amount: float) -> str:
    u = unit.strip().lower().rstrip(".")
    if not u:
        return ""
    plural = amount > 1 or (amount != 0 and amount != 1 and amount > 0)  # 1.5 cups, 2 cans; 1 can; 0.5 cup
    if amount and 0 < amount < 1:
        plural = False
    key = u if u in UNITS else u.rstrip("s") if u.rstrip("s") in UNITS else None
    if key is None:
        return unit.strip()
    sing, plur = UNITS[key]
    return plur if plural else sing


def _expand_abbr(text: str) -> str:
    return " ".join(_ABBR.get(w.lower().rstrip("."), w) for w in text.split())


@dataclass(frozen=True)
class Ingredient:
    amount: float
    unit: str
    food: str
    note: str = ""

    def spoken(self) -> str:
        """'2 pounds of chicken thighs' / 'half a cup of onion, diced' / '2 eggs' / 'salt, to taste'."""
        amt = _spoken_amount(self.amount)
        unit = _unit_words(self.unit, self.amount)
        food = _expand_abbr(self.food)
        if unit:
            joiner = " of " if unit.rstrip("s") in _OF_UNITS or unit in _OF_UNITS else " "
            if food.lower().startswith("of "):      # "3 cans of of diced tomatoes" (the food was entered with its own "of")
                joiner = " "
            text = f"{amt} {unit}{joiner}{food}".strip() if food else f"{amt} {unit}".strip()
        else:
            text = f"{amt} {food}".strip()
        return f"{text}, {_expand_abbr(self.note)}" if self.note else text


@dataclass(frozen=True)
class Recipe:
    id: int
    name: str
    servings: int
    servings_text: str
    ingredients: list[Ingredient]
    steps: list[str]
    keywords: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- API

def _client(settings: Settings) -> httpx.AsyncClient:
    if not settings.tandoor_token:
        raise SourceError("tandoor: no API token in the environment")
    return httpx.AsyncClient(
        base_url=settings.tandoor_url, timeout=8.0,
        headers={"Authorization": f"Bearer {settings.tandoor_token}", "Host": settings.tandoor_host},
    )


def _spoken_name(name: str) -> str:
    """'Cincinnati / Skyline / 5 Way Chili' -> 'Cincinnati, Skyline, 5 Way Chili'."""
    return " ".join(re.sub(r"\s*/\s*", ", ", name).split())


async def search(settings: Settings, query: str, limit: int = 5) -> list[Hit]:
    """Names and keywords first; if nothing, by ingredient (Tandoor's `query`
    does not look at foods — "something with eggplant" needs the food index)."""
    try:
        async with _client(settings) as c:
            resp = await c.get("/api/recipe/", params={"query": query, "page_size": limit})
            resp.raise_for_status()
            rows = resp.json().get("results", [])
            by_ingredient = False
            if not rows:
                by_ingredient = True
                foods = await c.get("/api/food/", params={"query": query, "page_size": 3})
                foods.raise_for_status()
                ids = [str(f["id"]) for f in foods.json().get("results", [])]
                if ids:
                    resp = await c.get("/api/recipe/", params={"foods": ids, "foods_or": 1, "page_size": limit})
                    resp.raise_for_status()
                    rows = resp.json().get("results", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"tandoor: {exc}") from exc
    return [Hit(int(r["id"]), _spoken_name(r["name"]), by_ingredient) for r in rows]


async def get(settings: Settings, recipe_id: int) -> Recipe:
    try:
        async with _client(settings) as c:
            resp = await c.get(f"/api/recipe/{recipe_id}/")
        resp.raise_for_status()
        r = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"tandoor: {exc}") from exc
    ingredients: list[Ingredient] = []
    steps: list[str] = []
    for step in r.get("steps", []):
        for ing in step.get("ingredients", []):
            if ing.get("is_header"):
                continue
            ingredients.append(Ingredient(
                amount=float(ing.get("amount") or 0),
                unit=((ing.get("unit") or {}).get("name") or "").strip(),
                food=((ing.get("food") or {}).get("name") or "").strip(),
                note=(ing.get("note") or "").strip(),
            ))
        text = _clean(step.get("instruction") or "")
        if text:
            steps.append(text)
    return Recipe(
        id=int(r["id"]), name=_spoken_name(r["name"]), servings=int(r.get("servings") or 0),
        servings_text=(r.get("servings_text") or "").strip(), ingredients=ingredients, steps=steps,
        keywords=[k.get("name", "") for k in r.get("keywords", [])],
    )


# ---------------------------------------------------------------- phrasing

def _clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # markdown images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = text.replace("**", "").replace("*", "").replace("#", "")
    return " ".join(text.split())


_FRACTIONS = {0.25: "a quarter", 0.5: "half a", 0.75: "three quarters of a", 0.33: "a third of a", 0.67: "two thirds of a",
              1.5: "one and a half", 2.5: "two and a half"}


def _spoken_amount(n: float) -> str:
    if not n:
        return ""
    if n == int(n):
        return str(int(n))
    for k, v in _FRACTIONS.items():
        if abs(n - k) < 0.02:
            return v
    return f"{n:g}"


def hits_text(hits: list[Hit], query: str) -> str:
    if not hits:
        return f"Nothing in Tandoor for {query}."
    how = f" with {query} in the ingredients" if hits[0].by_ingredient else ""
    if len(hits) == 1:
        return f"One{how}: {hits[0].name}. Say ingredients or steps."
    names = ". ".join(f"{i}: {h.name}" for i, h in enumerate(hits, 1))
    return f"{len(hits)} recipes{how}. {names}. Say a number, then ingredients or steps."


def ingredients_text(r: Recipe) -> str:
    if not r.ingredients:
        return f"{r.name} has no ingredient list in Tandoor."
    serves = f" Serves {r.servings}{(' ' + r.servings_text) if r.servings_text else ''}." if r.servings else ""
    items = ". ".join(i.spoken() for i in r.ingredients)
    return f"{r.name}.{serves} {len(r.ingredients)} ingredients: {items}."


def step_text(r: Recipe, n: int) -> str | None:
    """Step n (0-based) as 'Step 3 of 7. <text>'; None past the end."""
    if n < 0 or n >= len(r.steps):
        return None
    return f"Step {n + 1} of {len(r.steps)}. {r.steps[n]}"


# ---------------------------------------------------------------- the caller's words

_SEARCH = re.compile(
    r"^(?:do (?:i|we) have (?:a |any )?recipes? (?:for|with|using)|(?:find|search|look up|look for) (?:a |me a )?recipes? (?:for|with|using)?"
    r"|(?:what|which) recipes? (?:do (?:i|we) have )?(?:for|with|use|using)|(?:any|got) recipes? (?:for|with|using)"
    r"|something (?:with|using)|recipes? (?:for|with|using))\s+(.+?)[.?!]?$", re.IGNORECASE)
_INGREDIENTS = re.compile(r"^(?:(?:the |what are the )?ingredients?(?: list)?(?: for| of| in)?)\s*(.*?)[.?!]?$", re.IGNORECASE)
_STEPS = re.compile(r"^(?:(?:the |read (?:me )?the )?(?:steps|instructions|directions|method|recipe steps)(?: for| of)?)\s*(.*?)[.?!]?$", re.IGNORECASE)
_NUM = r"(one|two|three|four|five|first|second|third|fourth|fifth|1|2|3|4|5)(?:st|nd|rd|th)?"
_PICK = re.compile(rf"^(?:number |option |the )?{_NUM}(?: one)?[.?!]?$", re.IGNORECASE)
# "three, ingredients" / "number two steps" — a pick and an action in one breath
_PICK_THEN = re.compile(rf"^(?:number |option |the )?{_NUM}(?: one)?[,.]?\s+(ingredients?|steps|instructions|directions)[.?!]?$", re.IGNORECASE)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def _num(w: str) -> str:
    return str(_WORDS.get(w.lower(), w))


def parse(transcript: str) -> tuple[str, str] | None:
    """('search'|'ingredients'|'steps'|'pick'|'pick-then'|'next-step', argument) or None.
    pick-then's argument is 'N:ingredients' or 'N:steps'."""
    t = transcript.strip()
    if re.fullmatch(r"next step|next[.?!]?", t, re.IGNORECASE):
        return ("next-step", "")
    if m := _PICK_THEN.match(t):
        action = "steps" if m.group(2).lower().startswith(("step", "instr", "direc")) else "ingredients"
        return ("pick-then", f"{_num(m.group(1))}:{action}")
    if m := _SEARCH.match(t):
        return ("search", m.group(1).strip())
    if m := _INGREDIENTS.match(t):
        return ("ingredients", m.group(1).strip())
    if m := _STEPS.match(t):
        return ("steps", m.group(1).strip())
    if m := _PICK.match(t):
        return ("pick", _num(m.group(1)))
    return None


def closest(hits: list[Hit], name: str) -> Hit | None:
    """The hit whose name best overlaps the caller's words — for "ingredients
    for Killy" when whisper mangled "chili" but three chilis are on the table."""
    q = {w[:5] for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2}
    if not q or not hits:
        return None
    scored = sorted(((len(q & {w[:5] for w in re.findall(r"[a-z0-9]+", h.name.lower())}), h) for h in hits), key=lambda s: -s[0])
    if scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None      # no overlap, or a tie ("chili" matches all three): let a real search decide
    return scored[0][1]
