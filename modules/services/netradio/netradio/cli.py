import sys

from netradio import admin, compile as compile_, dj, liq, playlists, profile, profile_server, wake

COMMANDS = {"playlists": playlists.main, "wake": wake.main, "dj": dj.main,
            "profile": profile.main, "profile-server": profile_server.main,
            "admin": admin.main, "liq": liq.main,
            "compile-prompt": compile_.main_prompt, "compile-apply": compile_.main_apply}


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: netradio {{{'|'.join(COMMANDS)}}} [options]", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
