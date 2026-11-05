#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PhiTest.py

计算公式：

    w_i = L_i^p / sqrt(n_k)

    S = sum(w_i * A_i) / sum(w_i)

其中：

    A_i：第 i 次击打的准确率，范围为 0～100
    L_i：第 i 张谱面的等级
    n_k：同一张谱面在本次测试中出现的次数
    p：等级权重系数，默认值为 1
    S：最终成绩，范围为 0～100

重复谱面通过 sqrt(n_k) 进行权重折扣。
"""

from __future__ import annotations

import json
import math
import os
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


# =========================
# 基本配置
# =========================

MAX_RECORDS_PER_SESSION = 1500
MIN_UNIQUE_CHARTS = 3
DEFAULT_POWER = 1.0

BASE_DIR = Path(__file__).resolve().parent
RECORDS_FILE = BASE_DIR / "records.txt"
SESSIONS_FILE = BASE_DIR / "sessions.txt"


# =========================
# 通用工具
# =========================

def clear_screen() -> None:
    """清理终端屏幕。"""
    os.system("cls" if os.name == "nt" else "clear")


def pause() -> None:
    input("\n按 Enter 键继续...")


def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value: str, field_name: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise ValueError(f"{field_name}必须是数字。")

    if not math.isfinite(result):
        raise ValueError(f"{field_name}必须是有效数字。")

    return result


def ensure_data_files() -> None:
    """如果数据文件不存在，则创建空文件。"""
    RECORDS_FILE.touch(exist_ok=True)
    SESSIONS_FILE.touch(exist_ok=True)


def append_json_line(file_path: Path, data: dict[str, Any]) -> None:
    """以 JSON Lines 格式追加一行数据。"""
    with file_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_json_lines(file_path: Path) -> list[dict[str, Any]]:
    """读取 JSON Lines 文件。忽略损坏的行。"""
    if not file_path.exists():
        return []

    result = []

    with file_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)

                if isinstance(data, dict):
                    result.append(data)

            except json.JSONDecodeError:
                print(
                    f"警告：{file_path.name} 第 {line_number} 行无法读取，已跳过。",
                    file=sys.stderr
                )

    return result


def format_score(score: float | None) -> str:
    if score is None:
        return "无"

    return f"{score:.2f}"


# =========================
# 公式计算
# =========================

def calculate_score(
    records: list[dict[str, Any]],
    power: float = DEFAULT_POWER
) -> dict[str, Any]:
    """
    根据公式计算本次测试成绩。

    w_i = L_i^p / sqrt(n_k)
    S = sum(w_i * A_i) / sum(w_i)
    """

    if not records:
        raise ValueError("没有可计算的成绩记录。")

    if power <= 0:
        raise ValueError("等级权重系数必须大于 0。")

    chart_counts = Counter(
        record["chart_name"]
        for record in records
    )

    weighted_score_sum = 0.0
    weight_sum = 0.0

    for record in records:
        accuracy = float(record["accuracy"])
        level = float(record["level"])
        chart_name = record["chart_name"]

        count = chart_counts[chart_name]

        # 重复谱面折扣
        repeat_discount = math.sqrt(count)

        # 等级权重
        weight = (level ** power) / repeat_discount

        weighted_score_sum += weight * accuracy
        weight_sum += weight

    if weight_sum == 0:
        raise ValueError("总权重为 0，无法计算成绩。")

    score = weighted_score_sum / weight_sum

    unique_charts = len(chart_counts)

    return {
        "score": score,
        "record_count": len(records),
        "unique_chart_count": unique_charts,
        "is_official": unique_charts >= MIN_UNIQUE_CHARTS,
        "power": power,
        "chart_counts": dict(chart_counts),
    }


# =========================
# 输入功能
# =========================

def input_chart_record(index: int) -> dict[str, Any] | None:
    """
    输入一条谱面成绩。

    返回 None 表示用户结束本次输入。
    """

    print(f"\n第 {index} / {MAX_RECORDS_PER_SESSION} 条记录")
    print("输入 q 可以结束本次测试。")

    chart_name = input("谱面名称：").strip()

    if chart_name.lower() == "q":
        return None

    if not chart_name:
        print("谱面名称不能为空。")
        return None

    while True:
        level_text = input("谱面等级，例如 12、14.7：").strip()

        if level_text.lower() == "q":
            return None

        try:
            level = safe_float(level_text, "谱面等级")

            if level <= 0:
                print("谱面等级必须大于 0。")
                continue

            break

        except ValueError as error:
            print(error)

    while True:
        accuracy_text = input("准确率，范围 0～100：").strip()

        if accuracy_text.lower() == "q":
            return None

        try:
            accuracy = safe_float(accuracy_text, "准确率")

            if accuracy < 0 or accuracy > 100:
                print("准确率必须在 0～100 之间。")
                continue

            break

        except ValueError as error:
            print(error)

    return {
        "chart_name": chart_name,
        "level": level,
        "accuracy": accuracy,
    }


def input_power() -> float:
    """输入等级权重系数 p。"""
    while True:
        text = input(
            f"等级权重系数 p，直接 Enter 使用默认值 {DEFAULT_POWER}："
        ).strip()

        if not text:
            return DEFAULT_POWER

        try:
            power = safe_float(text, "等级权重系数")

            if power <= 0:
                print("等级权重系数必须大于 0。")
                continue

            return power

        except ValueError as error:
            print(error)


# =========================
# 新建测试
# =========================

def create_new_session() -> None:
    clear_screen()

    print("=" * 50)
    print("新建 PhiTest 测试")
    print("=" * 50)
    print("至少需要 3 张不同谱面才能得到正式判断。")
    print(f"一次最多可以记录 {MAX_RECORDS_PER_SESSION} 条击打记录。")
    print("允许重复谱面，但重复谱面的权重会自动降低。\n")

    power = input_power()
    records: list[dict[str, Any]] = []

    while len(records) < MAX_RECORDS_PER_SESSION:
        record = input_chart_record(len(records) + 1)

        if record is None:
            break

        records.append(record)

        print(
            f"已记录 {len(records)} 条，"
            f"当前不同谱面数："
            f"{len(set(item['chart_name'] for item in records))}"
        )

    if not records:
        print("\n没有输入任何记录，本次测试取消。")
        pause()
        return

    calculation = calculate_score(records, power)

    session_id = str(uuid.uuid4())
    created_at = now_string()

    # 为每条成绩添加 session_id 和时间
    for record in records:
        saved_record = {
            "session_id": session_id,
            "created_at": created_at,
            **record,
        }

        append_json_line(RECORDS_FILE, saved_record)

    session_data = {
        "session_id": session_id,
        "created_at": created_at,
        "score": calculation["score"],
        "record_count": calculation["record_count"],
        "unique_chart_count": calculation["unique_chart_count"],
        "is_official": calculation["is_official"],
        "power": power,
    }

    append_json_line(SESSIONS_FILE, session_data)

    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)
    print(f"本次记录数：{calculation['record_count']}")
    print(f"不同谱面数：{calculation['unique_chart_count']}")
    print(f"等级权重系数 p：{power}")
    print(f"测试成绩：{calculation['score']:.2f} / 100")

    if calculation["is_official"]:
        print("结果状态：正式结果")
    else:
        print(
            f"结果状态：样本不足，至少需要 "
            f"{MIN_UNIQUE_CHARTS} 张不同谱面"
        )

    print("\n谱面重复次数：")
    for chart_name, count in calculation["chart_counts"].items():
        print(f"  {chart_name}: {count} 次")

    pause()


# =========================
# 历史记录
# =========================

def show_history() -> None:
    clear_screen()

    print("=" * 70)
    print("历史测试记录")
    print("=" * 70)

    sessions = read_json_lines(SESSIONS_FILE)

    if not sessions:
        print("暂时没有历史记录。")
        pause()
        return

    for index, session in enumerate(sessions, start=1):
        status = "正式" if session.get("is_official") else "样本不足"

        print(
            f"{index:>3}. "
            f"{session.get('created_at', '未知时间')} | "
            f"成绩：{format_score(session.get('score'))} | "
            f"记录：{session.get('record_count', 0)} | "
            f"不同谱面：{session.get('unique_chart_count', 0)} | "
            f"状态：{status}"
        )

    pause()


def load_session_records(session_id: str) -> list[dict[str, Any]]:
    all_records = read_json_lines(RECORDS_FILE)

    return [
        record
        for record in all_records
        if record.get("session_id") == session_id
    ]


def show_session_detail() -> None:
    clear_screen()

    sessions = read_json_lines(SESSIONS_FILE)

    if not sessions:
        print("暂时没有历史记录。")
        pause()
        return

    print("=" * 50)
    print("查看测试详情")
    print("=" * 50)

    for index, session in enumerate(sessions, start=1):
        print(
            f"{index}. "
            f"{session.get('created_at', '未知时间')} - "
            f"{format_score(session.get('score'))} 分"
        )

    print("输入 q 返回。")

    choice = input("请选择测试编号：").strip()

    if choice.lower() == "q":
        return

    try:
        index = int(choice)

        if index < 1 or index > len(sessions):
            raise ValueError

    except ValueError:
        print("无效的编号。")
        pause()
        return

    session = sessions[index - 1]
    records = load_session_records(session["session_id"])

    print("\n" + "=" * 70)
    print(f"测试时间：{session.get('created_at', '未知')}")
    print(f"测试成绩：{format_score(session.get('score'))} / 100")
    print("=" * 70)

    if not records:
        print("找不到该测试的详细谱面记录。")
        pause()
        return

    print(f"{'序号':<6}{'谱面名称':<30}{'等级':<10}{'准确率':<10}")

    for index, record in enumerate(records, start=1):
        print(
            f"{index:<6}"
            f"{record['chart_name']:<30}"
            f"{record['level']:<10.2f}"
            f"{record['accuracy']:<10.2f}"
        )

    pause()


# =========================
# 绘图和比较
# =========================

def plot_history() -> None:
    clear_screen()

    sessions = read_json_lines(SESSIONS_FILE)

    if len(sessions) < 1:
        print("没有足够的历史数据可以绘图。")
        pause()
        return

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("当前环境没有安装 matplotlib。")
        print("可以使用以下命令安装：")
        print("pip install matplotlib")
        pause()
        return

    scores = []
    labels = []

    for index, session in enumerate(sessions, start=1):
        score = session.get("score")

        if score is None:
            continue

        scores.append(float(score))
        labels.append(
            session.get("created_at", f"Test {index}")
        )

    if not scores:
        print("没有有效成绩可以绘图。")
        pause()
        return

    plt.figure(figsize=(11, 6))
    plt.plot(
        range(1, len(scores) + 1),
        scores,
        marker="o",
        linewidth=2
    )

    for index, score in enumerate(scores, start=1):
        plt.annotate(
            f"{score:.2f}",
            (index, score),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center"
        )

    plt.title("PhiTest History Score Plot")
    plt.xlabel("Attempts")
    plt.ylabel("Score / 100")
    plt.ylim(0, 100)
    plt.xticks(range(1, len(scores) + 1))
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()

    output_file = BASE_DIR / "phitest_history.png"
    plt.savefig(output_file, dpi=150)

    print(f"图表已保存到：{output_file}")

    try:
        plt.show()
    except Exception:
        pass

    pause()


def compare_sessions() -> None:
    clear_screen()

    sessions = read_json_lines(SESSIONS_FILE)

    if len(sessions) < 2:
        print("至少需要两次测试才能比较。")
        pause()
        return

    print("=" * 60)
    print("选择要比较的测试")
    print("=" * 60)

    for index, session in enumerate(sessions, start=1):
        print(
            f"{index}. "
            f"{session.get('created_at', '未知时间')} - "
            f"{format_score(session.get('score'))} 分"
        )

    first_text = input("\n第一次测试编号：").strip()
    second_text = input("第二次测试编号：").strip()

    try:
        first_index = int(first_text)
        second_index = int(second_text)

        if not (1 <= first_index <= len(sessions)):
            raise ValueError

        if not (1 <= second_index <= len(sessions)):
            raise ValueError

    except ValueError:
        print("编号无效。")
        pause()
        return

    first = sessions[first_index - 1]
    second = sessions[second_index - 1]

    first_score = float(first["score"])
    second_score = float(second["score"])
    difference = second_score - first_score

    print("\n" + "=" * 60)
    print("测试结果比较")
    print("=" * 60)
    print(
        f"第一次：{first.get('created_at', '未知时间')} - "
        f"{first_score:.2f} 分"
    )
    print(
        f"第二次：{second.get('created_at', '未知时间')} - "
        f"{second_score:.2f} 分"
    )

    if difference > 0:
        print(f"变化：提升 {difference:.2f} 分")
    elif difference < 0:
        print(f"变化：下降 {abs(difference):.2f} 分")
    else:
        print("变化：没有变化")

    pause()


# =========================
# 数据管理
# =========================

def show_data_location() -> None:
    clear_screen()

    print("=" * 50)
    print("数据文件位置")
    print("=" * 50)
    print(f"击打记录：{RECORDS_FILE}")
    print(f"测试记录：{SESSIONS_FILE}")
    print(f"历史图表：{BASE_DIR / 'phitest_history.png'}")

    pause()


def clear_history() -> None:
    clear_screen()

    print("=" * 50)
    print("清空历史数据")
    print("=" * 50)
    print("此操作会删除所有击打记录和测试结果。")

    choice = input("输入 DELETE 确认清空：").strip()

    if choice != "DELETE":
        print("已取消。")
        pause()
        return

    RECORDS_FILE.write_text("", encoding="utf-8")
    SESSIONS_FILE.write_text("", encoding="utf-8")

    print("历史数据已清空。")
    pause()


# =========================
# 主菜单
# =========================

def print_menu() -> None:
    print("=" * 50)
    print("PhiTest.py")
    print("=" * 50)
    print("1. 新建测试")
    print("2. 查看历史测试")
    print("3. 查看测试详情")
    print("4. 比较两次测试")
    print("5. 绘制历史成绩")
    print("6. 查看数据文件位置")
    print("7. 清空历史数据")
    print("0. 退出")
    print("=" * 50)


def main() -> None:
    ensure_data_files()

    while True:
        clear_screen()
        print_menu()

        choice = input("请选择功能：").strip()

        if choice == "1":
            create_new_session()

        elif choice == "2":
            show_history()

        elif choice == "3":
            show_session_detail()

        elif choice == "4":
            compare_sessions()

        elif choice == "5":
            plot_history()

        elif choice == "6":
            show_data_location()

        elif choice == "7":
            clear_history()

        elif choice == "0":
            print("感谢使用 PhiTest。")
            break

        else:
            print("无效选项。")
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程序已退出。")
    except Exception as error:
        print(f"\n程序发生错误：{error}")
        pause()