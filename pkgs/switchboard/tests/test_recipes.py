import asyncio

import pytest

from switchboard import agi as agi_mod, intents, recipes
from switchboard.config import Settings


def test_parse_phrasings() -> None:
    assert recipes.parse("Do I have a recipe for white chili?") == ("search", "white chili")
    assert recipes.parse("something with eggplant") == ("search", "eggplant")
    assert recipes.parse("any recipes with chicken thighs") == ("search-ingredient", "chicken thighs")   # "with" = by ingredient now
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
    assert recipes.ingredients_text(r) == ("White Chicken Chili. Serves 6 bowls. 4 ingredients: 2 pounds of chicken thighs. half a cup of onion, diced. "
                                           "1 can of white beans. salt, to taste.")
    assert recipes.step_text(r, 0) == "Step 1 of 2. Brown the chicken."
    assert recipes.step_text(r, 2) is None
    assert recipes.hits_text([], "durian") == "Nothing in Tandoor for durian."
    assert recipes.hits_text([recipes.Hit(1, "Chili")], "chili") == "One: Chili. Say ingredients or steps."
    assert recipes.hits_text([recipes.Hit(1, "A"), recipes.Hit(2, "B")], "x") == "2 recipes. 1: A. 2: B. Say a number, then ingredients or steps."


def test_recipe_turn_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """search -> pick -> ingredients -> steps -> next -> next -> end, all against a fake Tandoor."""
    book = {1: recipes.Recipe(1, "White Chicken Chili", 6, "", [recipes.Ingredient(2, "lb", "chicken")], ["Brown it.", "Simmer."]),
            2: recipes.Recipe(2, "Green Chili Stew", 4, "", [], ["Stew."])}

    async def fake_search(settings, q, limit=5, by_ingredient=False):
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
    assert say("ingredients") == "White Chicken Chili. Serves 6. 1 ingredients: 2 pounds of chicken."
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


def test_pick_then_and_closest() -> None:
    assert recipes.parse("Three ingredients.") == ("pick-then", "3:ingredients")
    assert recipes.parse("number two, steps") == ("pick-then", "2:steps")
    assert recipes.parse("Number three.") == ("pick", "3")
    assert recipes.parse("Next.") == ("next-step", "")
    hits = [recipes.Hit(1, "Cincinnati, Skyline, 5 Way Chili"), recipes.Hit(2, "Grandma's Chili"), recipes.Hit(3, "Southwest White Chili")]
    assert recipes.closest(hits, "White Chicken Chili").id == 3
    assert recipes.closest(hits, "Skyline Chili").id == 1
    assert recipes.closest(hits, "Killy") is None          # too mangled: falls back to a real search
    assert recipes.closest(hits, "chili") is None          # a tie across all three: not a pick
    assert recipes.closest([], "chili") is None


def test_live_recipe_context_owns_picks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """After a search, 'Number three.' / 'three ingredients' must reach the recipe flow, not the agent."""
    book = {1: recipes.Recipe(1, "Alpha Chili", 1, "", [recipes.Ingredient(1, "cup", "beans")], ["Cook."]),
            2: recipes.Recipe(2, "Bravo Chili", 1, "", [], ["Stir."]), 3: recipes.Recipe(3, "Charlie Chili", 1, "", [recipes.Ingredient(2, "", "eggs")], ["Fry."])}

    async def fake_search(settings, q, limit=5, by_ingredient=False):
        return [recipes.Hit(i, r.name) for i, r in book.items()]

    async def fake_get(settings, rid):
        return book[rid]

    monkeypatch.setattr(recipes, "search", fake_search)
    monkeypatch.setattr(recipes, "get", fake_get)
    board = agi_mod.Switchboard(Settings(state_dir=tmp_path))

    class C:
        id = "k"

    say = lambda t: asyncio.run(board.recipe_turn(C(), t)).text   # noqa: E731
    say("do I have a recipe for chili")
    assert say("Three ingredients.") == "Charlie Chili. Serves 1. 1 ingredients: 2 eggs."
    assert say("Number two.") == "Bravo Chili. Say ingredients or steps."
    assert say("ingredients for charlie chili") == "Charlie Chili. Serves 1. 1 ingredients: 2 eggs."   # closest hit, no new search
    # and the router-level rule: with hits live, a bare pick is a recipe intent
    assert intents.route("Number three.") is None                      # the pure router still doesn't know...
    rs = board.recipe_state["k"]
    assert rs["hits"] and recipes.parse("Number three.") is not None    # ...but the AGI's guard does


def test_units_are_spoken_not_spelled() -> None:
    I = recipes.Ingredient
    assert I(2, "lb", "ground beef").spoken() == "2 pounds of ground beef"
    assert I(1, "lb", "venison").spoken() == "1 pound of venison"
    assert I(1, "tsp", "salt").spoken() == "1 teaspoon of salt"
    assert I(2, "tsp", "cumin").spoken() == "2 teaspoons of cumin"
    assert I(0.5, "tsp", "pepper").spoken() == "half a teaspoon of pepper"
    assert I(1.5, "cups", "flour").spoken() == "one and a half cups of flour"
    assert I(32, "oz", "tomato sauce").spoken() == "32 ounces of tomato sauce"
    assert I(1, "can", "white beans").spoken() == "1 can of white beans"
    assert I(2, "cans", "beans").spoken() == "2 cans of beans"
    assert I(1, "qt", "water").spoken() == "1 quart of water"
    assert I(1, "pkg", "taco seasoning").spoken() == "1 package of taco seasoning"
    assert I(3, "", "eggs").spoken() == "3 eggs"
    assert I(1, "lg", "onion, chopped").spoken() == "1 lg onion, chopped"     # unknown unit passes through
    assert I(2, "", "lg onions", "approx").spoken() == "2 large onions, about"
    assert I(1, "medium", "onion").spoken() == "1 medium onion"                 # not a container: no "of"
    assert I(3, "cans", "of diced tomatoes").spoken() == "3 cans of diced tomatoes"   # food entered with its own "of"


INDEX = [
    recipes.Entry(1, "Cincinnati / Skyline / 5 Way Chili", ["soup"], ["ground beef", "cumin", "tomato sauce", "onion"]),
    recipes.Entry(2, "Grandma's Chili", [], ["kidney beans", "burger", "chili powder"]),
    recipes.Entry(3, "The BEST Ground Venison Tacos", ["mexican"], ["ground venison", "cumin", "chili powder", "onion"]),
    recipes.Entry(4, "Hamburger Minestrone Soup", [], ["ground beef", "carrots", "onion"]),
    recipes.Entry(5, "Amish Chicken", [], ["chicken thighs", "butter"]),
    recipes.Entry(6, "Best Damn Instant Pot Pulled Pork", [], ["pork shoulder", "chicken broth", "ground cumin"]),
]


def test_index_search_names_then_ingredients() -> None:
    names = lambda hits: [h.name for h in hits]   # noqa: E731
    assert names(recipes.search_index(INDEX, "chili")) == ["Cincinnati, Skyline, 5 Way Chili", "Grandma's Chili"]
    assert names(recipes.search_index(INDEX, "skyline chili"))[0] == "Cincinnati, Skyline, 5 Way Chili"
    assert names(recipes.search_index(INDEX, "chicken")) == ["Amish Chicken"]                          # by name
    hits = recipes.search_index(INDEX, "chicken", by_ingredient=True)
    assert names(hits) == ["Amish Chicken", "Best Damn Instant Pot Pulled Pork"] and all(h.by_ingredient for h in hits)
    hits = recipes.search_index(INDEX, "cumin")                       # no name match -> foods, 'ground cumin' counts
    assert names(hits) == ["Best Damn Instant Pot Pulled Pork", "Cincinnati, Skyline, 5 Way Chili", "The BEST Ground Venison Tacos"]
    assert recipes.search_index(INDEX, "ground beef", by_ingredient=True) and recipes.search_index(INDEX, "lemonade") == []
    assert names(recipes.search_index(INDEX, "one")) == []            # "ingredients for one" must never find Minestrone


def test_ingredient_search_phrasings_and_number_picks() -> None:
    assert recipes.parse("Recipes that use cumin.") == ("search-ingredient", "cumin")
    assert recipes.parse("what recipes have chicken thighs") == ("search-ingredient", "chicken thighs")
    assert recipes.parse("what can I make with venison") == ("search-ingredient", "venison")
    assert recipes.parse("Recipes with chicken.") == ("search-ingredient", "chicken")
    assert recipes.parse("ingredients for one.") == ("pick-then", "1:ingredients")
    assert recipes.parse("steps for the second one") == ("pick-then", "2:steps")
    assert recipes.parse("ingredients for good gravy") == ("ingredients", "good gravy")


def test_hits_text_counts_beyond_the_five_read() -> None:
    hits = [recipes.Hit(i, f"R{i}", True) for i in range(1, 6)]
    assert recipes.hits_text(hits, "cumin", total=12).startswith("12 recipes with cumin in the ingredients, the first 5. 1: R1.")
    assert recipes.hits_text(hits, "cumin", total=5).startswith("5 recipes with cumin in the ingredients. 1: R1.")
