"""Alembic scripts, deliberately inside the package.

They must ship with the code: the service runs migrations at every start, and
locating them relative to a source checkout crash-looped the first deploy.
Being a real package is what guarantees setuptools installs the .py files —
package-data globs into subdirectories are unreliable under pyproject builds.
"""
