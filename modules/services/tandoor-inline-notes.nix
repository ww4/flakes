# Tandoor: ingredient notes inline, not hidden behind a comment bubble.
#
# Tandoor shows an ingredient's note as a click-to-open tooltip hanging off a
# `far fa-comment` icon. The note text is only written into the DOM in PRINT
# mode, so no stylesheet alone can reveal it — the text simply isn't there.
#
# The component already contains exactly the markup we want; it just gates it
# behind a print-mode check:
#
#     isPrintMode ? <td><span class="text-disabled font-italic">note</span></td> : nothing
#     isPrintMode ? nothing : <td><bubble icon + tooltip></td>
#
# So forcing that one expression to `true` in this one asset gives us the text
# cell and drops the bubble. A little CSS then pulls the cell against the food
# name with a leading comma, so a line reads "1 cup onion, chopped" the way a
# printed cookbook sets it.
#
# Why the regex rather than an exact string: the bundle is minified and the
# identifiers are renamed on every release (2.6.9 emitted `g(D)().isPrintMode`,
# 2.6.13 emits `l(P)().isPrintMode`). Matching the SHAPE survives that; matching
# the text would break on every upgrade.
#
# The patch is scoped to IngredientsTable-*.js on purpose. Two other assets
# reference isPrintMode and must keep their real print behaviour.
#
# (The greps are wrapped in `|| true` because stdenv runs phases under
# `set -eu -o pipefail`, and a grep matching nothing would abort the build
# before the message below could print.)
#
# It still fails LOUDLY if Tandoor restructures this component: the build
# asserts exactly two occurrences before and zero after. If that assertion
# trips on an upgrade, open the new assets/IngredientsTable-*.js and re-check
# the two ternaries above before adjusting.
#
# One component feeds the recipe ingredient list, the per-step lists and the
# book view, so patching it once covers all three.
#
# The service module lives in homelab-modules (hm.tandoor) and leaves
# `package` at its default, so this override needs no mkForce.
{ pkgs, ... }:

let
  frontend = pkgs.tandoor-recipes.frontend.overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      f=$(echo "$out"/assets/IngredientsTable-*.js)
      [ -f "$f" ] || { echo "inline-notes: IngredientsTable asset not found in $out/assets"; exit 1; }
      n=$({ grep -o 'isPrintMode' "$f" || true; } | wc -l)
      [ "$n" = 2 ] || { echo "inline-notes: expected 2 isPrintMode uses, found $n in $f"; exit 1; }
      sed -i -E 's/[A-Za-z_$][A-Za-z0-9_$]*\([A-Za-z_$][A-Za-z0-9_$]*\)\(\)\.isPrintMode/!0/g' "$f"
      m=$({ grep -o 'isPrintMode' "$f" || true; } | wc -l)
      [ "$m" = 0 ] || { echo "inline-notes: $m isPrintMode uses survived the patch in $f"; exit 1; }
    '';
  });

  css = ''
    /* ingredient notes inline — modules/services/tandoor-inline-notes.nix */
    .v-table td:has(> span.text-disabled.font-italic){width:100%;padding-left:0!important}
    .v-table td:has(> span.text-disabled.font-italic)>span{color:inherit!important;font-style:normal!important}
    .v-table td:has(> span.text-disabled.font-italic)>span:not(:empty)::before{content:", "}
    .v-table td:has(+ td>span.text-disabled.font-italic){width:1px;white-space:nowrap;padding-right:0!important}
  '';
in
{
  services.tandoor-recipes.package = pkgs.tandoor-recipes.overrideAttrs (old: {
    # Style the note cell so it reads as part of the ingredient line.
    postPatch = (old.postPatch or "") + ''
      substituteInPlace cookbook/templates/frontend/tandoor.html \
        --replace-fail '</head>' '<style>${css}</style>
      </head>'
    '';

    # Re-point the vue3 symlink at the patched frontend before collectstatic
    # runs, so Django hashes and pre-compresses the patched file itself rather
    # than us having to rewrite its manifest afterwards.
    postConfigure = (old.postConfigure or "") + ''
      rm -f cookbook/static/vue3
      ln -s ${frontend}/ cookbook/static/vue3
    '';
  });
}
