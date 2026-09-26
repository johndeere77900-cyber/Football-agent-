"""
Generates a clean, readable dashboard as a Markdown file (DASHBOARD.md).
GitHub renders Markdown nicely right in the repo's file view - no separate
hosting needed, and it stays completely private since it's just a file in
your existing private repo.
"""

import sqlite3
from datetime import datetime, timezone

import config


def _connect():
    return sqlite3.connect(config.DB_PATH)


def _football_today_and_recent():
    conn = _connect()
    rows = conn.execute("""
        SELECT home_team, away_team, league, top_pick, top_probability,
               confidence_label, match_date
        FROM predictions
        ORDER BY created_at DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    return rows


def _basketball_today_and_recent():
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT home_team, away_team, league, top_pick, top_probability,
                   confidence_label, game_date
            FROM basketball_predictions
            ORDER BY created_at DESC
            LIMIT 20
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def _football_accuracy():
    conn = _connect()
    rows = conn.execute("""
        SELECT confidence_label, top_pick_correct
        FROM predictions WHERE top_pick_correct IS NOT NULL
    """).fetchall()
    conn.close()
    return rows


def _basketball_accuracy():
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT confidence_label, top_pick_correct
            FROM basketball_predictions WHERE top_pick_correct IS NOT NULL
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def _accuracy_block(rows, title):
    if not rows:
        return f"### {title}\n\n_No graded predictions yet._\n"

    total = len(rows)
    correct = sum(r[1] for r in rows)
    overall = correct / total

    lines = [f"### {title}\n", f"**Overall accuracy:** {overall:.0%} ({correct}/{total} graded)\n"]
    lines.append("| Confidence | Accuracy | Count |")
    lines.append("|---|---|---|")
    for label, emoji in [("High", "🟢"), ("Moderate", "🟡"), ("Toss-up", "🔴")]:
        subset = [r[1] for r in rows if r[0] == label]
        if subset:
            acc = sum(subset) / len(subset)
            lines.append(f"| {emoji} {label} | {acc:.0%} | {len(subset)} |")
    return "\n".join(lines) + "\n"


def _predictions_table(rows, emoji):
    if not rows:
        return "_No recent predictions._\n"

    conf_emoji = {"High": "🟢", "Moderate": "🟡", "Toss-up": "🔴"}
    lines = ["| Match | League | Pick | Confidence |", "|---|---|---|---|"]
    for home, away, league, pick, prob, conf, date in rows:
        c_emoji = conf_emoji.get(conf, "")
        lines.append(f"| {home} vs {away} | {league} | {pick} ({prob:.0%}) | {c_emoji} {conf} |")
    return "\n".join(lines) + "\n"


def generate():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    football_rows = _football_today_and_recent()
    basketball_rows = _basketball_today_and_recent()
    football_acc = _football_accuracy()
    basketball_acc = _basketball_accuracy()

    content = f"""# 📊 Sports Prediction Dashboard

_Last updated: {now}_

## ⚽ Recent Football Predictions

{_predictions_table(football_rows, "⚽")}

## 🏀 Recent Basketball Predictions

{_predictions_table(basketball_rows, "🏀")}

## 📈 Football Accuracy

{_accuracy_block(football_acc, "Football Track Record")}

## 📈 Basketball Accuracy

{_accuracy_block(basketball_acc, "Basketball Track Record")}

---
_This file updates automatically after each prediction run._
"""

    with open("DASHBOARD.md", "w") as f:
        f.write(content)

    print("Dashboard generated: DASHBOARD.md")


if __name__ == "__main__":
    generate()
