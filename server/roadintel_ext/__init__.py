"""Optional road intelligence. Legacy imports never load SUMO or PyOsmium."""
VERSION = 2


def availability():
    try:
        from .sumo import executable
        return True, str(executable())
    except (ImportError, OSError, ValueError) as exc:
        return False, f"SUMO unavailable: {exc}; install server/requirements-roadintel-ext.txt"
