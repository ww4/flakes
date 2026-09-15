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


@dataclass(frozen=True)
class Ingredient:
    amount: float
    unit: str
    food: str
    note: str = ""

    def spoken(self) -> str:
        amt = _spoken_amount(self.amount)
        parts = [p for p in (amt, self.unit, self.food) if p]
        text = " ".join(parts)
        return f"{text}, {self.note}" if self.note else text


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
_PICK = re.compile(r"^(?:number |option |the )?(one|two|three|four|five|first|second|third|fourth|fifth|1|2|3|4|5)(?:st|nd|rd|th)?(?: one)?$", re.IGNORECASE)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def parse(transcript: str) -> tuple[str, str] | None:
    """('search'|'ingredients'|'steps'|'pick'|'next-step', argument) or None."""
    t = transcript.strip()
    if re.fullmatch(r"next step|next", t, re.IGNORECASE):
        return ("next-step", "")
    if m := _SEARCH.match(t):
        return ("search", m.group(1).strip())
    if m := _INGREDIENTS.match(t):
        return ("ingredients", m.group(1).strip())
    if m := _STEPS.match(t):
        return ("steps", m.group(1).strip())
    if m := _PICK.match(t):
        w = m.group(1).lower()
        return ("pick", str(_WORDS.get(w, w)))
    return None
