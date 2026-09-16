import sys

from netradio import dj, playlists, profile, wake

COMMANDS = {"playlists": playlists.main, "wake": wake.main, "dj": dj.main, "profile": profile.main}


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: netradio {{{'|'.join(COMMANDS)}}} [options]", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
