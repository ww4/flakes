import asyncio

import pytest

from switchboard import agi as agi_mod, intents, recipes
from switchboard.config import Settings


def test_parse_phrasings() -> None:
    assert recipes.parse("Do I have a recipe for white chili?") == ("search", "white chili")
    assert recipes.parse("something with eggplant") == ("search", "eggplant")
    assert recipes.parse("any recipes with chicken thighs") == ("search", "chicken thighs")
    assert recipes.parse("ingredients for the white chicken chili") == ("ingredients", "the white chicken chili")
    assert recipes.parse("Ingredients.") == ("ingredients", "")
    assert recipes.parse("steps") == ("steps", "")
    assert recipes.parse("read me the instructions for the chili") == ("steps", "the chili")
    assert recipes.parse("next step") == ("next-step", "")
    assert recipes.parse("number two") == ("pick", "2")
    assert recipes.parse("the first one") == ("pick", "1")
    assert recipes.parse("what's the weather") is None
    assert intents.route("do I have a recipe for chili") == "recipe"
    assert intents.route("something with eggplant") == "recipe"
    assert intents.route("ingredients") == "recipe"
    assert intents.route("steps") == "recipe"


def test_phrasing() -> None:
    r = recipes.Recipe(1, "White Chicken Chili", 6, "bowls", [
        recipes.Ingredient(2, "lb", "chicken thighs"), recipes.Ingredient(0.5, "cup", "onion", "diced"),
        recipes.Ingredient(1, "can", "white beans"), recipes.Ingredient(0, "", "salt", "to taste")],
        ["Brown the chicken.", "Add the onion and beans; simmer 30 minutes."])
    assert recipes.ingredients_text(r) == ("White Chicken Chili. Serves 6 bowls. 4 ingredients: 2 lb chicken thighs. half a cup onion, diced. "
                                           "1 can white beans. salt, to taste.")
    assert recipes.step_text(r, 0) == "Step 1 of 2. Brown the chicken."
    assert recipes.step_text(r, 2) is None
    assert recipes.hits_text([], "durian") == "Nothing in Tandoor for durian."
    assert recipes.hits_text([recipes.Hit(1, "Chili")], "chili") == "One: Chili. Say ingredients or steps."
    assert recipes.hits_text([recipes.Hit(1, "A"), recipes.Hit(2, "B")], "x") == "2 recipes. 1: A. 2: B. Say a number, then ingredients or steps."


def test_recipe_turn_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """search -> pick -> ingredients -> steps -> next -> next -> end, all against a fake Tandoor."""
    book = {1: recipes.Recipe(1, "White Chicken Chili", 6, "", [recipes.Ingredient(2, "lb", "chicken")], ["Brown it.", "Simmer."]),
            2: recipes.Recipe(2, "Green Chili Stew", 4, "", [], ["Stew."])}

    async def fake_search(settings, q, limit=5):
        return [recipes.Hit(i, r.name) for i, r in book.items() if q.split()[-1].lower() in r.name.lower()]

    async def fake_get(settings, rid):
        return book[rid]

    monkeypatch.setattr(recipes, "search", fake_search)
    monkeypatch.setattr(recipes, "get", fake_get)
    board = agi_mod.Switchboard(Settings(state_dir=tmp_path))

    class C:
        id = "call1"

    say = lambda t: asyncio.run(board.recipe_turn(C(), t)).text   # noqa: E731
    assert say("do I have a recipe for chili") == "2 recipes. 1: White Chicken Chili. 2: Green Chili Stew. Say a number, then ingredients or steps."
    assert say("number one") == "White Chicken Chili. Say ingredients or steps."
    assert say("ingredients") == "White Chicken Chili. Serves 6. 1 ingredients: 2 lb chicken."
    assert say("steps") == "Step 1 of 2. Brown it."
    assert say("next step") == "Step 2 of 2. Simmer."
    assert say("next step") == "That was the last step of White Chicken Chili."
    assert say("do I have a recipe for durian") == "Nothing in Tandoor for durian."
    assert say("ingredients") == "Which recipe? Say do I have a recipe for, and a dish."   # the miss cleared the selection
    assert say("number three") == "Search for a recipe first, then pick a number."


def test_lookup_failure_is_spoken(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    board = agi_mod.Switchboard(Settings(state_dir=tmp_path))   # no token -> SourceError

    class C:
        id = "c"

    assert asyncio.run(board.recipe_turn(C(), "do I have a recipe for chili")).text == "I couldn't reach the recipe book right now."


def test_spoken_name_drops_slashes() -> None:
    assert recipes._spoken_name("Cincinnati / Skyline / 5 Way Chili") == "Cincinnati, Skyline, 5 Way Chili"
    assert recipes._spoken_name("Plain Name") == "Plain Name"


def test_hits_text_says_when_it_matched_by_ingredient() -> None:
    assert recipes.hits_text([recipes.Hit(1, "Tacos", True)], "venison") == "One with venison in the ingredients: Tacos. Say ingredients or steps."
