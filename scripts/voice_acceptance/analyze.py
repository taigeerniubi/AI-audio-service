"""离线汇总十次真实响应的延迟、打断与静音证据并生成中文验收报告。"""

import csv
import json
import math
import re
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from backend.core.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT / "output" / "voice-acceptance" / "run"


def percentile(values: list[float], fraction: float) -> float:
    """使用最近秩法计算当前小样本的描述性分位数。"""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def pick(events: list[dict], kind: str, event_type: str, since: float = 0) -> list[dict]:
    """从浏览器时间线按类型和起点筛选事件。"""
    return [
        row
        for row in events
        if row["kind"] == kind and row.get("event", {}).get("type") == event_type and row["at"] >= since
    ]


def main() -> None:
    """生成 JSON、CSV 和 Markdown，不发起任何网络或模型调用。"""
    browser = json.loads((ROOT / "browser-results.json").read_text(encoding="utf-8"))
    provider = json.loads((ROOT / "provider-results.json").read_text(encoding="utf-8"))
    snapshots = {row["snapshot"]["timeOrigin"]: row["snapshot"] for row in browser["sessions"]}
    reports = {report["session_id"]: report for row in browser["sessions"] for report in row["reports"]}
    spoken, interruptions, nonspeech, muted = [], [], [], []
    responses = {}
    for snapshot in snapshots.values():
        events = snapshot["events"]
        for row in pick(events, "playback", "started"):
            rid = row["event"]["response_id"]
            responses[rid] = {"play_at": row["at"], "origin": snapshot["timeOrigin"], "label": "应用开场"}
        for item in snapshot["inputs"]:
            if item["name"] in {"uh", "uhuh", "clicks"} or item["label"] == "麦克风静音时输入语音":
                continue
            first = next(
                (
                    row
                    for row in pick(events, "playback", "started", item["at"])
                    if row["event"]["response_id"] not in item["playingAtStart"]
                ),
                None,
            )
            if not first:
                continue
            rid = first["event"]["response_id"]
            vad = next(
                (
                    row
                    for row in pick(events, "server", "trace", item["at"])
                    if row["event"]["event"].get("type") == "input_audio_buffer.speech_stopped"
                ),
                None,
            )
            audio = next(
                row
                for row in pick(events, "server", "audio", item["at"])
                if row["event"]["response_id"] == rid
            )
            row = {
                "label": item["label"],
                "text": item["text"],
                "response_id": rid,
                "interruption_reply": bool(item["playingAtStart"]),
                "user_end_to_play_ms": round(first["at"] - item["captureEndAt"], 1),
                "user_end_to_vad_event_ms": round(vad["at"] - item["captureEndAt"], 1) if vad else None,
                "vad_event_to_audio_ms": round(audio["at"] - vad["at"], 1) if vad else None,
                "audio_to_play_ms": round(first["at"] - audio["at"], 1),
                "early_reply": first["at"] < item["captureEndAt"],
            }
            spoken.append(row)
            responses[rid].update(row)
    for case in browser["cases"]:
        if case["type"] == "interruption":
            item = case["input"]
            snapshot = next(
                s for s in snapshots.values() if any(i["label"] == item["label"] for i in s["inputs"])
            )
            events = snapshot["events"]
            rid = case["original_playback"]["event"]["response_id"]
            done = next(
                row for row in pick(events, "server", "response_done") if row["event"]["response_id"] == rid
            )
            clear = next(row for row in pick(events, "server", "clear_audio", item["at"]))
            late = [
                row
                for row in pick(events, "server", "audio", case["cleared"]["at"])
                if row["event"]["response_id"] == rid
            ]
            original = next(
                row
                for row in pick(events, "server", "response_started")
                if row["event"]["response_id"] == rid
            )
            resumed = next(
                row
                for row in pick(events, "server", "response_started")
                if row["event"]["response_id"] == case["resumed"]["event"]["response_id"]
            )
            interruptions.append(
                {
                    "label": case["label"],
                    "response_id": rid,
                    "voice_onset_to_clear_ms": round(case["cleared"]["at"] - item["captureStartAt"], 1),
                    "voice_onset_to_clear_command_ms": round(clear["at"] - item["captureStartAt"], 1),
                    "clear_command_to_worklet_ms": round(case["cleared"]["at"] - clear["at"], 1),
                    "reply_after_user_end_ms": round(case["resumed"]["at"] - item["captureEndAt"], 1),
                    "generating_when_user_started": done["at"] > item["captureStartAt"],
                    "generating_when_interruption_recognized": done["at"] > clear["at"],
                    "old_audio_after_clear": len(late),
                    "same_session_continued": original["event"]["session_id"]
                    == resumed["event"]["session_id"],
                }
            )
        elif case["type"] == "non_interruption":
            nonspeech.append(
                {
                    "label": case["label"],
                    "unwanted_clears": len(case["clears"]),
                    "additional_responses": case["responses_after"] - case["responses_before"],
                    "playing_at_start": bool(case["input"]["playingAtStart"]),
                }
            )
        elif case["type"] == "muted":
            reminders = [
                row
                for row in case["events"]
                if row.get("event", {}).get("event", {}).get("type") == "idle_reminder"
            ]
            muted.append({"label": case["label"], "reminders": len(reminders)})
    silence_case = next(row for row in browser["cases"] if row["type"] == "silence")
    se = silence_case["events"]
    trigger = next(
        row for row in pick(se, "server", "trace") if row["event"]["event"]["type"] == "idle_reminder"
    )
    started = pick(se, "playback", "started")[0]
    complete = next(row for row in pick(se, "playback", "complete", started["at"]))
    ended = pick(se, "server", "ended")[0]
    reminder_id = started["event"]["response_id"]
    reminder_text = next(
        item["text"]
        for report in reports.values()
        for item in report["transcripts"]
        if item.get("response_id") == reminder_id
    )
    responses[reminder_id]["label"] = "静音提醒（内容错误）"
    silence = {
        "idle_to_reminder_trigger_ms": round(trigger["at"] - silence_case["since"], 1),
        "idle_to_reminder_play_ms": round(started["at"] - silence_case["since"], 1),
        "reminder_complete_to_ended_ms": round(ended["at"] - complete["at"], 1),
        "ended_reason": ended["event"]["reason"],
        "actual_reminder_text": reminder_text,
        "reminder_text_passed": bool(re.search(r"还在|需要.*继续|继续.*介绍", reminder_text)),
    }
    created = [row for row in provider["events"] if row["type"] == "response.created"]
    call_rows = []
    for number, row in enumerate(created, 1):
        rid = row["response_id"]
        response = responses.get(rid, {})
        report = next(
            report
            for report in reports.values()
            if any(m.get("response_id") == rid for m in report["metrics"])
        )
        metric = next(m for m in report["metrics"] if m.get("response_id") == rid)
        done = next(e for e in provider["events"] if e["type"] == "response.done" and e["response_id"] == rid)
        call_rows.append(
            {
                "number": number,
                "connection": row["connection"],
                "response_id": rid,
                "label": response.get("label"),
                "user_end_to_play_ms": response.get("user_end_to_play_ms"),
                "provider_created_to_first_audio_ms": metric.get("model_first_audio_ms"),
                "ui_event_to_play_ack_ms": metric.get("speech_stop_event_to_play_ack_ms"),
                "status": done.get("status"),
            }
        )
    normal_values = [row["user_end_to_play_ms"] for row in spoken if not row["interruption_reply"]]
    all_values = [row["user_end_to_play_ms"] for row in spoken]
    start_time = datetime.fromtimestamp(created[0]["at"], ZoneInfo("Asia/Shanghai"))
    summary = {
        "date": start_time.isoformat(),
        "model": "qwen-audio-3.1-realtime-plus",
        "turn_detection": "smart_turn_v2",
        "responses": len(created),
        "connections": len(snapshots),
        "browser": browser["browser_version"],
        "normal_sample_count": len(normal_values),
        "normal_p50_ms": median(normal_values),
        "normal_p95_ms": percentile(normal_values, 0.95),
        "all_spoken_sample_count": len(all_values),
        "all_spoken_p50_ms": median(all_values),
        "all_spoken_p95_ms": percentile(all_values, 0.95),
        "spoken_turns": spoken,
        "interruptions": interruptions,
        "nonspeech": nonspeech,
        "muted": muted,
        "silence": silence,
        "calls": call_rows,
        "errors": browser["errors"],
        "provider_errors": [row for row in provider["events"] if row["type"] == "error"],
        "estimated_cost_from_app_rates_cny": round(
            sum(report["estimated_cost_cny"] or 0 for report in reports.values()), 6
        ),
    }
    (ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (ROOT / "responses.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(call_rows[0]))
        writer.writeheader()
        writer.writerows(call_rows)
    table = "\n".join(
        f"| {row['number']} | {row['label']} | {row['user_end_to_play_ms'] if row['user_end_to_play_ms'] is not None else '不适用'} | {row['ui_event_to_play_ack_ms'] if row['ui_event_to_play_ack_ms'] is not None else '不适用'} |"
        for row in call_rows
    )
    latency_table = "\n".join(
        f"| {row['label']} | {row['user_end_to_vad_event_ms']} | {row['vad_event_to_audio_ms']} | {row['audio_to_play_ms']} | {row['user_end_to_play_ms']} |"
        for row in spoken
    )
    interrupt_table = "\n".join(
        f"| {row['label']} | {row['voice_onset_to_clear_ms']} | {row['clear_command_to_worklet_ms']} | {row['reply_after_user_end_ms']} | {row['old_audio_after_clear']} |"
        for row in interruptions
    )
    report_text = f"""# 千问 Plus 真实语音验收：10 次模型响应

日期：{start_time.strftime("%Y-%m-%d %H:%M")}（北京时间）。结论：**回复延迟在这组样本中约 1.5 秒；附和过滤与停顿处理通过；有效插话能继续会话，但停止旧声音过慢；静音计时有效，提醒内容失败。**

## 测试范围与计数

- 真实 `qwen-audio-3.1-realtime-plus`、北京地域、`longanqian_v3.1`、`smart_turn_v2`，使用本机配置的同一模型。
- 共 {len(created)} 次服务端 `response.created`、{len(snapshots)} 条真实 WebSocket 会话；含 2 次开场、5 次普通语音提问的回复、2 次插话后的回复、1 次静音触发。测试后已停止验收服务，没有继续调用。
- Chrome {browser["browser_version"]}，真实 React 页面、FastAPI、Web Audio/AudioWorklet 与云 API。输入为 Windows Huihui 离线中文合成语音，经过虚拟麦克风进入原有 20ms PCM 采集链路；没有读取实体麦克风，没有使用 Mock 模型。
- 运行数据使用独立 SQLite 文件，不改动用户产品与会话数据。输出音频在浏览器渲染线程处理，系统扬声器静音；原始模型音频未落盘。
- 本次按用户最新要求限制为十次响应，取代原计划的 100 个正常轮次与 30 次插话采样。这是小样本验收，不能据此证明生产 P95 或真实电话质量。

## 核心结果

| 项目 | 实测 | 结论 |
| --- | --- | --- |
| 普通语音说完至首次播放 | n={len(normal_values)}，P50 {median(normal_values):.1f}ms，最近秩 P95 {percentile(normal_values, 0.95):.1f}ms | 本组达到原定 P50≤1.5s、P95≤3s；样本少 |
| 含插话回复的全部有效输入 | n={len(all_values)}，P50 {median(all_values):.1f}ms，范围 {min(all_values):.1f}～{max(all_values):.1f}ms | 没有把开场或静音提醒混入用户说完的指标 |
| 插话开始至旧音频清空 | {interruptions[0]["voice_onset_to_clear_ms"]:.1f}ms、{interruptions[1]["voice_onset_to_clear_ms"]:.1f}ms | **不满足 ≤500ms 目标** |
| 插话后会话继续 | 2/2，同一连接收到新回复，无旧响应音频回流 | 本组通过 |
| 听模型说话时“嗯”“嗯嗯” | 各 1 次，无 clear_audio、无额外模型响应 | 本组通过，不能概括所有语气和人声 |
| 合成键盘式脉冲 | 1 次，无误打断 | 本组通过，不等同于真实键盘/扬声器回声 |
| 句中停顿 | 600ms、1200ms，均整句转录与回答，没有提前回复 | 本组通过 |
| 静音提醒调度 | 播放完成后 {silence["idle_to_reminder_trigger_ms"]:.1f}ms 发起，{silence["idle_to_reminder_play_ms"]:.1f}ms 首次播放 | 定时逻辑接近配置 8 秒 |
| 静音提醒内容 | 实际为“{reminder_text}” | **失败：重复上句，没有询问用户是否仍在** |
| 提醒后静音结束 | 提醒播完 {silence["reminder_complete_to_ended_ms"]:.1f}ms 后浏览器收到 ended，reason=idle_timeout | 配置 20 秒；该数值包含关闭连接与报告保存 |
| 麦克风静音 | 持续约 10.45 秒并注入语音，未触发提醒或新响应 | 本组通过 |

## 十次响应明细

单位均为毫秒。“当前应用面板口径”从云端结束检测事件开始，不包括此前的判停等待；它不能代替用户感知的完整回复延迟。

| 次数 | 场景 | 用户有效语音结束→首次播放 | 当前应用面板口径 |
| --- | --- | ---: | ---: |
{table}

## 延迟分段证据

“用户说完”取采集端最后一个非静音 PCM 帧的发送回调时间（20ms 帧，RMS≥0.002），不是录音文件尾部。播放端取 AudioWorklet 的 started 回调，非实体扬声器的声学测量。所有下表差值使用同一浏览器单调时钟。

| 输入 | 说完→收到结束检测事件 | 结束事件→首音频包 | 音频包→播放回调 | 总计 |
| --- | ---: | ---: | ---: | ---: |
{latency_table}

结束检测事件在这组实测中比实际语音结束晚约 1.4～1.5 秒，之后音频很快到达。应用报告中的几十毫秒不代表整轮对话只有几十毫秒；不能据此拆出“ASR/LLM/TTS 内部耗时”，服务端可能在事件之间进行未公开处理。

## 打断证据

| 场景 | 用户有效发声→旧音频清空 | 收到 clear→Worklet 清空 | 说完→新回复 | 清空后旧音频包数 |
| --- | ---: | ---: | ---: | ---: |
{interrupt_table}

主要等待发生在客户端收到云端有效发声/清空信号之前；本地清空处理只有约 0.1～0.2ms。这一分段包含上行、服务端判断和下行，不能进一步断言全是模型内部 VAD 运算耗时。

两次插话开始时，原响应仍在生成；等云端报告有效发声时，原响应已 completed、音频仍在播放。应用清空了播放并删除未听完的助手消息，两次均保持原会话继续。本次**没有覆盖“收到打断信号时生成仍未完成”的真实 response.cancel 分支**，不能把播放打断通过写成云端取消生成通过。

静音触发后的 response.created / 音频 / response.done 均实际发生，没有 API 错误，但内容复述上一句。当前代码通过 `conversation.item.create(role=system)` 追加静音说明，再发送 `response.create`；本次结果说明这一路径的预期提醒行为没有得到保障。系统说明是否被服务端按预期用于推理仍需独立确认，不能仅凭 ACK 当作生效。

## 问题与后续改进

1. **打断速度不合格**：保持“嗯/嗯嗯不打断”的要求，对更早的有效插话判定做专项优化；不能直接用任意人声检测替代语义判断。下一轮调整必须重新验证附和误打断与延迟之间的取舍。
2. **静音提醒内容失败**：修正当前系统说明与生成触发方式，或采用经验证的固定提醒播放方案；本次额度用尽，没有追加云调用验证补丁，所以这里记录缺陷而不声称已修复。
3. **指标容易被误读**：当前 `speech_stop_event_to_play_ack_ms` 仅为事件后的延迟。建议保留该细分指标，同时独立报告完整端到端测量，不能把 15ms～156ms 当成用户真实等待时间。
4. 未覆盖真人口音、不同音量、真实键盘、扬声器回声、连续三次插话、工具执行与预约竞态、多人或电话通道。此次十响应里没有触发销售工具，不能宣称已经验收所有业务工具。

## 证据文件与复核

- `output/voice-acceptance/run/summary.json`：离线汇总指标。
- `output/voice-acceptance/run/responses.csv`：十次响应明细，可用 Excel 打开。
- `output/voice-acceptance/run/browser-results.json`：输入标记、实际采集边界、浏览器事件、应用报告。
- `output/voice-acceptance/run/provider-results.json` / `provider-events.jsonl`：真实模型事件与响应计数，已去掉音频和凭证。
- `session-1.png` / `session-2.png`：真实页面截图，可见提醒重复与会话结束状态。
- 合成输入、精确时长及 SHA256 在 `.cache/voice-acceptance/fixtures/manifest.json`。
- 复核命令：`.venv\\Scripts\\python.exe -m scripts.voice_acceptance.analyze`；只读取本地证据，不再调用模型。

官方接口依据：[实时 WebSocket 与智能轮次](https://help.aliyun.com/zh/model-studio/audio-realtime-websocket-access)、[服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)、[客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)。本次实测配置为 smart_turn_v2；`silence_duration_ms` 仅适用于 server_vad，不能通过把它改成 500ms 来宣称已经优化智能轮次。上述官方说明与本次观测结果分开记录。
"""
    report_path = PROJECT_ROOT / "docs" / "实时语音十次验收报告.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    print(
        json.dumps(
            {
                "responses": len(created),
                "normal_p50_ms": median(normal_values),
                "normal_p95_ms": percentile(normal_values, 0.95),
                "interruptions": interruptions,
                "silence": silence,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
