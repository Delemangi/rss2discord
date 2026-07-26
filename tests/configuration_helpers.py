from pathlib import Path


def write_config(path: Path, feeds: str) -> None:
    path.write_text(
        "refresh_interval: 60\n"
        "delay_between_posts: 0\n"
        "max_post_age_days: 7\n"
        f"feeds:\n{feeds}",
    )
