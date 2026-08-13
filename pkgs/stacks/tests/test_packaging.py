"""The installed package must carry everything the service starts with.

The service crash-looped 1797 times on its first deploy because migrations were
located relative to a source checkout — a path that exists in a git clone and
not in the Nix store. Nothing tested the installed layout, so nothing caught it.
"""

from __future__ import annotations

from pathlib import Path


class TestMigrationsTravelWithTheCode:
    def test_scripts_live_inside_the_package(self):
        from stacks.db import migrations_dir

        d = migrations_dir()
        assert d.is_dir(), f"migration scripts missing: {d}"
        # Beside the code, not above it: anything resolved from a repository
        # root is absent once the package is installed.
        import stacks

        assert d.parent == Path(stacks.__file__).resolve().parent

    def test_env_and_versions_are_present(self):
        from stacks.db import migrations_dir

        d = migrations_dir()
        assert (d / "env.py").is_file(), "alembic env.py not packaged"
        versions = list((d / "versions").glob("*.py"))
        assert versions, "no migration versions packaged"

    def test_alembic_can_read_the_script_directory(self):
        """The exact call that failed on deploy."""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from stacks.db import migrations_dir

        cfg = Config()
        cfg.set_main_option("script_location", str(migrations_dir()))
        script = ScriptDirectory.from_config(cfg)
        assert script.get_current_head(), "alembic found no head revision"

    def test_migrations_are_real_packages(self):
        """Which is what actually gets the .py files installed.

        package-data globs into subdirectories are unreliable under pyproject
        builds — declaring "migrations/*.py" was not enough and the scripts
        were absent from the installed package. Being importable packages is.
        """
        from stacks.db import migrations_dir

        d = migrations_dir()
        assert (d / "__init__.py").is_file(), "migrations/ is not a package"
        assert (d / "versions" / "__init__.py").is_file(), "versions/ is not a package"

    def test_non_python_assets_are_declared(self):
        """The .mako template and the PWA are not .py and need listing."""
        import tomllib

        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "pyproject.toml").read_text())
        patterns = data["tool"]["setuptools"]["package-data"]["stacks"]
        for needed in ("web/*", "migrations/*.mako"):
            assert needed in patterns, f"{needed} not declared as package data"


class TestWebAssetsTravel:
    def test_pwa_files_are_inside_the_package(self):
        import stacks

        web = Path(stacks.__file__).resolve().parent / "web"
        for name in ("index.html", "app.js", "app.css", "sw.js", "book.html"):
            assert (web / name).is_file(), f"{name} would 404 on an installed copy"
