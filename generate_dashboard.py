"""
Generates a clean, readable dashboard as a Markdown file (DASHBOARD.md).
Uses the storage abstraction layer rather than direct database engine calls.
"""

from datetime import datetime, timezone

import storage


def _accuracy_block(summary, title):
    if not summary or summary.get("total_graded", 0) == 0:
        return f"### {title}\n\n_No graded predictions yet._\n"

    total = summary.get("total_graded", 0)
    overall = summary.get("overall_accuracy", 0.0)
    correct = round(total * overall)

    lines = [f"### {title}\n", f"**Overall accuracy:** {overall:.0%} ({correct}/{total} graded)\n"]
    lines.append("| Confidence | Accuracy | Count |")
    lines.append("|---|---|---|")

    by_confidence = summary.get("by_confidence", {})
    for label, emoji in [("High", "🟢"), ("Moderate", "🟡"), ("Toss-up", "🔴")]:
        if label in by_confidence:
            stats = by_confidence[label]
            acc = stats.get("accuracy", 0.0)
            count = stats.get("count", 0)
            lines.append(f"| {emoji} {label} | {acc:.0%} | {count} |")

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

    football_rows = storage.get_recent_predictions("football", limit=20)
    basketball_rows = storage.get_recent_predictions("basketball", limit=20)
    football_acc = storage.accuracy_summary()
    basketball_acc = storage.basketball_accuracy_summary()

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
