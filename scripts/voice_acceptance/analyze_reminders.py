"""离线汇总真实静音提醒复测的时间、实际话术和插话事件证据。"""

import csv
import json
import os

from backend.core.paths import PROJECT_ROOT


def pick(events: list[dict], kind: str, event_type: str, since: float = 0) -> list[dict]:
    """按浏览器单调时钟筛选事件。

    Args:
        events: 单个页面的脱敏事件。
        kind: server、playback 或 trace；trace 指服务端封装的观测事件。
        event_type: 要筛选的事件类型。
        since: 最早时间，单位为 performance.now 毫秒。

    Returns:
        按原顺序保留的匹配事件。
    """
    return [
        row
        for row in events
        if row["at"] >= since
        and (
            row["kind"] == "server"
            and row.get("event", {}).get("type") == "trace"
            and row["event"]["event"].get("type") == event_type
            if kind == "trace"
            else row["kind"] == kind and row.get("event", {}).get("type") == event_type
        )
    ]


def main() -> None:
    """读取已保存数据并导出 JSON 和 CSV；不连接模型或改写旧验收结果。"""
    run_name = os.environ.get("ACCEPTANCE_RUN", "reminder-retest-20261111")
    root = PROJECT_ROOT / "output" / "voice-acceptance" / run_name
    browser = json.loads((root / "browser-results.json").read_text(encoding="utf-8"))
    provider = json.loads((root / "provider-results.json").read_text(encoding="utf-8"))
    snapshots = {s["label"]: s["snapshot"] for s in browser["sessions"]}
    reports = [report for s in browser["sessions"] for report in s["reports"]]
    responses = {}
    for row in provider["events"]:
        rid = row.get("response_id")
        if row["type"] == "response.created":
            responses[rid] = {"response_id": rid, "connection": row["connection"], "created_utc": row["utc"]}
        elif row["type"] == "response.done":
            responses[rid].update(status=row["status"], usage=row["usage"])
        elif row["type"] == "response.audio_transcript.done":
            responses[rid]["text"] = row["transcript"]
    reminders, spoken, cases = [], [], []
    for label, snapshot in snapshots.items():
        events = snapshot["events"]
        for row in pick(events, "trace", "idle_reminder_result"):
            detail = row["event"]["event"]
            reminders.append({"label": label, **detail})
            responses[detail["response_id"]]["purpose"] = "idle_reminder"
        for item in snapshot["inputs"]:
            # 说完之后的首播放才可能是本次用户回复，排除竞争中的提醒。
            first = next(
                (
                    r
                    for r in pick(events, "playback", "started", item["captureEndAt"])
                    if r["event"]["response_id"] not in item["playingAtStart"]
                    and responses[r["event"]["response_id"]].get("purpose") != "idle_reminder"
                ),
                None,
            )
            if first:
                rid = first["event"]["response_id"]
                audio = next(r for r in pick(events, "server", "audio") if r["event"]["response_id"] == rid)
                spoken.append(
                    {
                        "session": label,
                        "label": item["label"],
                        "input_text": item["text"],
                        "response_id": rid,
                        "end_to_first_audio_ms": round(audio["at"] - item["captureEndAt"], 1),
                        "end_to_play_ms": round(first["at"] - item["captureEndAt"], 1),
                        "audio_to_play_ms": round(first["at"] - audio["at"], 1),
                    }
                )
        for row in responses.values():
            played = [
                r
                for r in pick(events, "playback", "started")
                if r["event"]["response_id"] == row["response_id"]
            ]
            if played:
                row.update(session=label, playback_started_ms=played[0]["at"])
    for case in browser["cases"]:
        result = {"type": case["type"], "label": case["label"]}
        events = snapshots[case["label"]]["events"]
        if case["type"] == "normal_reminder":
            result.update(
                idle_to_queued_ms=round(case["queued"]["at"] - case["previousEnd"]["at"], 1),
                idle_to_requested_ms=round(case["triggered"]["at"] - case["previousEnd"]["at"], 1),
                idle_to_play_ms=round(case["reminderFirst"]["at"] - case["previousEnd"]["at"], 1),
                playback_end_to_closed_ms=round(case["ended"]["at"] - case["reminderEnd"]["at"], 1),
                reason=case["ended"]["event"]["reason"],
                reminder=case["result"]["event"]["event"],
            )
        elif case["type"] == "reminder_interruption":
            rid = case["reminder"]["event"]["response_id"]
            clear = pick(events, "server", "clear_audio", case["input"]["at"])[0]
            generated = next(
                r for r in pick(events, "server", "response_done") if r["event"]["response_id"] == rid
            )
            result.update(
                voice_onset_to_clear_ms=round(case["cleared"]["at"] - case["input"]["captureStartAt"], 1),
                clear_command_to_worklet_ms=round(case["cleared"]["at"] - clear["at"], 1),
                user_end_to_reply_ms=round(case["resumed"]["at"] - case["input"]["captureEndAt"], 1),
                cleared_responses=case["cleared"]["event"].get("cleared_responses"),
                generating_at_voice_onset=generated["at"] > case["input"]["captureStartAt"],
                generating_at_clear=generated["at"] > clear["at"],
                old_audio_after_clear=sum(
                    r["event"]["response_id"] == rid
                    for r in pick(events, "server", "audio", case["cleared"]["at"])
                ),
                session_ids=list(
                    {r["event"]["session_id"] for r in events if r.get("event", {}).get("session_id")}
                ),
            )
        elif case["type"] == "boundary_speech":
            result.update(
                voice_onset_after_playback_end_ms=round(
                    case["input"]["captureStartAt"] - case["greetingEnd"]["at"], 1
                ),
                user_end_to_reply_ms=round(case["resumed"]["at"] - case["input"]["captureEndAt"], 1),
                reminder_events=[
                    r["event"]["event"]
                    for r in events
                    if r["at"] >= case["greetingEnd"]["at"]
                    and r.get("event", {}).get("type") == "trace"
                    and r["event"]["event"]["type"].startswith("idle_reminder")
                ],
                reminder_played_during_input=[
                    r
                    for r in pick(events, "playback", "started", case["input"]["at"])
                    if r["at"] < case["input"]["captureEndAt"]
                ],
            )
        cases.append(result)
    summary = {
        "run_name": run_name,
        "model": reports[0]["model"] if reports else None,
        "browser_version": browser.get("browser_version"),
        "response_count": provider["responses"],
        "session_count": len(snapshots),
        "provider_errors": [r for r in provider["events"] if r["type"] == "error"],
        "harness_errors": browser["errors"],
        "abnormal_socket_closes": [
            {"session": label, **row}
            for label, snapshot in snapshots.items()
            for row in snapshot["events"]
            if row["kind"] == "socket_close" and row.get("code") == 1006
        ],
        "actions": [action for report in reports for action in report.get("actions", [])],
        "reminders": reminders,
        "spoken": spoken,
        "cases": cases,
        "responses": list(responses.values()),
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "responses.csv").open("w", encoding="utf-8-sig", newline="") as target:
        columns = ["response_id", "connection", "session", "purpose", "status", "text", "created_utc"]
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(responses.values())
    print(json.dumps({k: v for k, v in summary.items() if k != "responses"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
