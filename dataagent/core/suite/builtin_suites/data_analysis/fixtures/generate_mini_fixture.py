"""Generate minimal CSV fixture for feature_engineer pipeline smoke tests (stdlib only)."""

from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path

N_USERS = 240
RNG = random.Random(42)

GAME_DIM_FILES = [
    "游戏本体数据_game_asset.csv",
    "游戏本体数据_game_brand.csv",
    "游戏本体数据_game_culture.csv",
    "游戏本体数据_game_feedback.csv",
    "游戏本体数据_game_growth.csv",
    "游戏本体数据_game_hardware.csv",
    "游戏本体数据_game_info.csv",
    "游戏本体数据_game_play.csv",
    "游戏本体数据_game_pro_sale.csv",
    "游戏本体数据_game_social.csv",
]

CITIES = ["北京", "上海", "成都", "南通", "未知市"]
GAMES = ["game_a", "game_b", "game_c"]


def _usids(n: int) -> list[str]:
    return [f"u{i:05d}" for i in range(n)]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="gbk", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_user_info(usids: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, usid in enumerate(usids):
        age = "" if index < 3 else RNG.randint(18, 55)
        gender = "" if 3 <= index < 6 else RNG.choice(["M", "F"])
        rows.append(
            {
                "usid": usid,
                "age": age,
                "gender": gender,
                "label": RNG.randint(0, 1),
                "city": RNG.choice(CITIES),
                "game_interest_play_u": RNG.choice(["game_a#game_b", "game_b", "game_c#game_a", ""]),
                "consumption_level": RNG.choice(["低", "中", "高"]),
                "occupation": RNG.choice(["学生", "白领", "自由"]),
            }
        )
    return rows


def build_pace_life(usids: list[str]) -> list[dict[str, object]]:
    return [{"usid": usid, "workday_freq": RNG.randint(0, 4), "weekend_freq": RNG.randint(0, 7)} for usid in usids]


def build_list_detail(usids: list[str]) -> list[dict[str, object]]:
    return [
        {
            "usid": usid,
            "app_pref_count": RNG.randint(1, 9),
            "top_app": RNG.choice(["app_x", "app_y", "app_z"]),
        }
        for usid in usids
    ]


def build_dev_info(usids: list[str]) -> list[dict[str, object]]:
    return [
        {
            "usid": usid,
            "dsid": usid,
            "device_brand": RNG.choice(["brand_a", "brand_b"]),
            "os_version": RNG.choice(["12", "13", "14"]),
        }
        for usid in usids
    ]


def build_push(usids: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for usid in usids:
        for _ in range(RNG.randint(1, 3)):
            rows.append(
                {
                    "usid": usid,
                    "exposure_cnt": RNG.randint(1, 19),
                    "click_cnt": RNG.randint(0, 4),
                }
            )
    return rows


def build_booking(usids: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for usid in usids:
        for game in RNG.sample(GAMES, k=RNG.randint(1, 2)):
            rows.append(
                {
                    "usid": usid,
                    "game_name": game,
                    "pay_amount": RNG.randint(0, 199),
                    "act_time": "2024-01-01",
                }
            )
    return rows


def build_detail(usids: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for usid in usids:
        for _ in range(RNG.randint(1, 2)):
            rows.append(
                {
                    "usid": usid,
                    "game_name": RNG.choice(GAMES),
                    "action_type": RNG.choice(["click", "install"]),
                    "media": RNG.choice(["feed", "banner"]),
                }
            )
    return rows


def build_game_dim(filename: str) -> list[dict[str, object]]:
    prefix = filename.replace("游戏本体数据_", "").replace(".csv", "")
    score_col = f"{prefix}_score"
    return [{"game_name": game, score_col: score} for game, score in zip(GAMES, [0.1, 0.5, 0.9], strict=True)]


def generate_fixture(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    usids = _usids(N_USERS)

    tables: dict[str, tuple[list[str], list[dict[str, object]]]] = {
        "user_info.csv": (
            ["usid", "age", "gender", "label", "city", "game_interest_play_u", "consumption_level", "occupation"],
            build_user_info(usids),
        ),
        "pace_life.csv": (["usid", "workday_freq", "weekend_freq"], build_pace_life(usids)),
        "list_detail_info.csv": (["usid", "app_pref_count", "top_app"], build_list_detail(usids)),
        "dev_info.csv": (["usid", "dsid", "device_brand", "os_version"], build_dev_info(usids)),
        "game_statistics_push.csv": (
            ["usid", "exposure_cnt", "click_cnt"],
            build_push(usids),
        ),
        "game_booking_pay_info.csv": (
            ["usid", "game_name", "pay_amount", "act_time"],
            build_booking(usids),
        ),
        "game_detail.csv": (
            ["usid", "game_name", "action_type", "media"],
            build_detail(usids),
        ),
    }
    for name, (fields, rows) in tables.items():
        _write_csv(target_dir / name, fields, rows)

    for dim_file in GAME_DIM_FILES:
        prefix = dim_file.replace("游戏本体数据_", "").replace(".csv", "")
        score_col = f"{prefix}_score"
        _write_csv(target_dir / dim_file, ["game_name", score_col], build_game_dim(dim_file))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "mini_yiwangzhihai",
    )
    args = parser.parse_args()
    generate_fixture(args.out)
    print(f"Generated fixture at {args.out}")


if __name__ == "__main__":
    main()
