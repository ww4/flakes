"""Regression tests for the co-failure clustering in netdiag-legs.

Gates the build. Both bugs below were real and both were found by a test with a
KNOWN answer rather than by reading the code:

  * pair keys were stored sorted and looked up unsorted, so most dotted-quad
    pairs silently scored 0 ("192.168.1.213" sorts before "192.168.1.67"). Three
    addresses failing in lockstep in every round were reported as a group of two.
  * the co-failure rate used `both / min(fails)`, which lets a chronically
    flapping device absorb any device whose rare outage lands inside one of its
    own. Craigmyle has a camera flapping ~20x/day, so that false grouping would
    have been the normal result, not an edge case.

A grouping tool that under-groups or over-groups is worse than no tool: it
argues, with apparent evidence, against the conclusion it exists to find.
"""
import collections
import importlib.machinery
import importlib.util
import sys


def load(path):
    """Import the BUILT script, which has no .py suffix.

    An explicit SourceFileLoader is required: spec_from_file_location infers the
    loader from the extension and returns a spec with loader=None for an
    extensionless file, which fails later with a confusing AttributeError about
    NoneType. Testing the built artifact rather than the source is deliberate --
    it exercises what writePython3Bin actually produced.
    """
    loader = importlib.machinery.SourceFileLoader("legs", path)
    spec = importlib.util.spec_from_loader("legs", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def build(rounds):
    fails, cofail = collections.Counter(), collections.Counter()
    for down in rounds:
        ds = sorted(down)
        for a in ds:
            fails[a] += 1
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                cofail[tuple(sorted((a, b)))] += 1
    return cofail, fails


def main():
    m = load(sys.argv[1])
    T = [(f"192.168.1.{i}", "x") for i in (10, 11, 12, 67, 213, 214)]
    A, B, C = "192.168.1.10", "192.168.1.11", "192.168.1.12"
    X, Y, Z = "192.168.1.67", "192.168.1.213", "192.168.1.214"
    cases = [
        ("lockstep trio is one shared leg", [{A, B, C}] * 8, [[A, B, C]]),
        ("independent faults are NOT grouped",
         [{A}, {B}, {C}] * 4, []),
        ("two legs stay two groups",
         [{A, B}] * 6 + [{Y, Z}] * 6, [[A, B], [Y, Z]]),
        ("a chronic flapper does not absorb a real pair",
         [{X}] * 20 + [{A, B, X}] * 2, [[A, B]]),
        ("pairs whose string order differs from natural order",
         [{X, Y}] * 6, [[Y, X]]),
        ("a single failing device is never a group", [{A}] * 9, []),
    ]
    bad = 0
    for label, rounds, expect in cases:
        cofail, fails = build(rounds)
        got = sorted(sorted(g) for g in m.cluster(cofail, fails, T, 0.6))
        want = sorted(sorted(g) for g in expect)
        if got == want:
            print(f"  ok   {label}")
        else:
            bad += 1
            print(f"  FAIL {label}\n       got  {got}\n       want {want}")
    if bad:
        print(f"\nnetdiag-legs: {bad} clustering regression(s)", file=sys.stderr)
        return 1
    print(f"  {len(cases)} clustering checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
