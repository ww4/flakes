# Tandoor frontend patches (gromit-only). Two of them, both applied to the
# compiled Vue bundle at build time:
#
#   1. ingredient notes inline, not hidden behind a comment bubble
#   2. auto-refresh an open recipe when it changes on the server
#
# They share one module because each has to override
# services.tandoor-recipes.package, and two modules setting it would
# conflict.
#
# --- 1. inline ingredient notes ---------------------------------------
#
# Tandoor shows an ingredient's note as a click-to-open tooltip hanging off a
# `far fa-comment` icon, and only writes the note into the DOM in PRINT mode —
# so no stylesheet alone can reveal it; the text simply isn't there.
#
# ./tandoor-inline-notes.py rewrites the compiled IngredientsTable component
# so the note is appended to the FOOD cell as ", <note>" — a line then reads
# "1 cup onion, chopped" the way a printed cookbook sets it — and suppresses
# both note cells (the print-mode one and the bubble). Putting it in the food
# cell rather than its own <td> is deliberate: a separate cell leaves table
# padding between the ingredient and the comma that CSS fights badly.
#
# The patcher locates everything by SHAPE, never literal text, because the
# bundle is minified and identifiers are renamed on every release (2.6.9
# emitted `g(D)().isPrintMode`, 2.6.13 emits `l(P)().isPrintMode`). It exits
# non-zero on anything unexpected, so a Tandoor upgrade that restructures the
# component FAILS THE BUILD rather than quietly restoring the bubbles. If that
# happens, read the script's error and the new assets/IngredientsTable-*.js.
#
# One component feeds the recipe ingredient list, the per-step lists and the
# book view, so patching it once covers all three.
#
# --- 2. auto-refresh ---------------------------------------------------
#
# Chris edits a recipe from a chat session while it is open on screen in the
# kitchen, hands covered in whatever he is cooking. ./tandoor-auto-refresh.py
# appends ./tandoor-auto-refresh.js to the Vue entry chunk: it polls the
# recipe's updated_at every 30 s, and on a change saves the scroll offset and
# reloads, putting you back at the same part of the page. Only the view route
# /recipe/<id> is watched, so an open editor is never reloaded mid-edit.
#
# The snippet is plain ES5 that touches nothing inside Tandoor, so unlike
# patch 1 it is not at risk from minified identifiers being renamed.
#
# The service module lives in homelab-modules (hm.tandoor) and leaves
# `package` at its default, so this override needs no mkForce.
{ pkgs, ... }:

let
  frontend = pkgs.tandoor-recipes.frontend.overrideAttrs (old: {
    nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.python3 ];
    postInstall = (old.postInstall or "") + ''
      python3 ${./tandoor-inline-notes.py} "$out"
      python3 ${./tandoor-auto-refresh.py} "$out" ${./tandoor-auto-refresh.js}
    '';
  });
in
{
  # Re-point the vue3 symlink at the patched frontend before collectstatic
  # runs, so Django hashes and pre-compresses the patched file itself rather
  # than us having to rewrite its manifest afterwards.
  services.tandoor-recipes.package = pkgs.tandoor-recipes.overrideAttrs (old: {
    postConfigure = (old.postConfigure or "") + ''
      rm -f cookbook/static/vue3
      ln -s ${frontend}/ cookbook/static/vue3
    '';
  });
}
