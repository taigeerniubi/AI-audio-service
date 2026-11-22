"""从浏览器与服务端原始事件重建工具问答延迟，分别报告首音频和有效声音。"""

import json
import os
from pathlib import Path


def analyze_session(session: dict, trial: int, provider: list[dict]) -> dict:
    """分析一次独立会话的检索回答，保留缺少有效声音探针的样本。

    Args:
        session: 包含浏览器时间线和最终报告的单次会话。
        trial: 此批次中的样本序号。
        provider: 全批次脱敏后的模型协议事件。

    Returns:
        同一浏览器时钟上的等待时间及独立的服务端工具往返时间。

    Raises:
        ValueError: 会话没有完成一次可关联的成功检索与回答。
    """
    snapshot = session["snapshot"]
    events = snapshot["events"]
    if len(snapshot["inputs"]) != 1:
        raise ValueError("每个隔离会话应包含且只包含一个测试问题")
    user_end = snapshot["inputs"][0]["captureEndAt"]
    tool = next(
        row for row in events
        if row["kind"] == "server" and row["event"]["type"] == "tool_result"
        and row["event"]["name"] == "search_product_knowledge"
    )
    if tool["event"].get("cancelled") or not tool["event"]["result"].get("matches"):
        raise ValueError("检索被取消或没有返回证据")
    answer = next(
        row for row in events if row["at"] >= tool["at"] and row["kind"] == "server"
        and row["event"]["type"] == "response_started"
    )
    rid = answer["event"]["response_id"]
    output = [row for row in events if row.get("event", {}).get("response_id") == rid]
    packet = next(row for row in output if row["kind"] == "server" and row["event"]["type"] == "audio")
    started = next(row for row in output if row["kind"] == "playback" and row["event"]["type"] == "started")
    audible = next((row for row in output if row["kind"] == "playback" and row["event"]["type"] == "audible"), None)
    first_speech = next(
        row for row in events if row["at"] >= user_end and row["kind"] == "playback"
        and row["event"]["type"] == ("audible" if audible else "started")
    )
    transcript = [
        row["event"]["message"]["text"] for row in events
        if row["kind"] == "server" and row["event"]["type"] == "transcript"
        and row["event"]["message"].get("response_id") == rid
    ][-1]
    connection = next(row["connection"] for row in provider if row.get("response_id") == rid)
    calls = [
        row for row in provider if row["connection"] == connection
        and row["type"] == "response.function_call_arguments.done"
    ]
    if len(calls) != 1:
        raise ValueError("此分析器要求每个会话只发生一次工具调用")
    call = calls[0]
    returned = next(
        row for row in provider if row.get("call_id") == call["call_id"]
        and row["type"] == "conversation.item.create" and row.get("item_type") == "function_call_output"
    )
    return {
        "trial": trial,
        "response_id": rid,
        "tool": call["name"],
        "matches": len(tool["event"]["result"]["matches"]),
        "transcript": transcript,
        "user_end_to_first_audio_packet_ms": round(packet["at"] - user_end, 1),
        "user_end_to_first_playback_ms": round(started["at"] - user_end, 1),
        "user_end_to_first_audible_ms": round(audible["at"] - user_end, 1) if audible else None,
        "user_end_to_tool_result_ms": round(tool["at"] - user_end, 1),
        "tool_result_to_audible_ms": round(audible["at"] - tool["at"], 1) if audible else None,
        "tool_arguments_to_result_send_ms": round((returned["monotonic"] - call["monotonic"]) * 1000, 1),
        "speech_before_tool_result": first_speech["at"] < tool["at"],
        "audible_measurement_available": audible is not None,
    }


def main() -> None:
    """读取已完成批次并生成 JSON 及本地 Markdown 报告，不发起模型调用。"""
    run_name = os.getenv("ACCEPTANCE_RUN", "tool-latency-20261111")
    folder = Path("output/voice-acceptance") / run_name
    browser = json.loads((folder / "browser-results.json").read_text(encoding="utf-8"))
    provider = json.loads((folder / "provider-results.json").read_text(encoding="utf-8"))
    rows = [analyze_session(session, i, provider["events"]) for i, session in enumerate(browser["sessions"], 1)]
    summary = {
        "run": run_name, "model_responses": provider["responses"], "browser_version": browser["browser_version"],
        "tool_questions": len(rows), "samples": rows, "harness_errors": browser["errors"],
        "provider_errors": [row for row in provider["events"] if row["type"] == "error"],
    }
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    table = "\n".join(
        f"| {row['trial']} | {row['user_end_to_first_audio_packet_ms']} | {row['user_end_to_first_playback_ms']} | "
        f"{row['user_end_to_first_audible_ms'] if row['audible_measurement_available'] else '未测得'} | "
        f"{row['tool_arguments_to_result_send_ms']} |" for row in rows
    )
    replies = "\n".join(f"- 样本 {row['trial']}：{row['transcript']}" for row in rows)
    missing = [str(row["trial"]) for row in rows if not row["audible_measurement_available"]]
    valid_count = len(rows) - len(missing)
    failure_note = (
        f"样本 {', '.join(missing)} 缺少有效声音探针，只有首 PCM 播放时间；不纳入有效声音统计。"
        if missing else "所有样本均取得有效声音探针回调。"
    )
    if any(error.get("message") == "Session ended before playback:audible" for error in browser["errors"]):
        failure_note += (
            "初次通过浏览器路由替换 AudioWorklet 未实际取得探针回调，等待期间多产生一次空闲提醒。"
            "改为测试服务直接返回探针脚本，先用本地 PCM 自检，再从已用四响应处继续，保留所有记录。"
        )
    report = f"""# 带检索工具的语音首音延迟实测

批次：{run_name}；模型 qwen-audio-3.1-realtime-plus；smart_turn_v2；{browser['browser_version']}。

在独立数据库中复制既有 Tally 产品及文档索引，使用本机离线合成语音提问：
“请查询产品资料，告诉我 Pro 套餐的月付价格是多少？”
每个问题单独创建会话，不复用前一问的检索答案；实际调用 search_product_knowledge 后继续生成回答。

## 实测结果

全部单位为毫秒。主要指标为最后有效输入帧至回答的首个有效声音。

| 样本 | 首音频到达浏览器 | 首 PCM 播放回调 | 首个有效声音回调 | 完整工具参数至结果发送 |
|---|---:|---:|---:|---:|
{table}

最后一列包含等待模型 response.done、检索、结果准备及本地提交，不等同于纯 Embedding 耗时。
共 {len(rows)} 次检索问答，逐样本返回片段数、回答文本与过渡语检查保存在 summary.json；首音始终关联检索后新响应。

{replies}

## 测量边界与失败记录

- 输入来自合成麦克风，起点为真实采集 Worklet 最后一个 RMS >= 0.002 的 20 ms 帧。
- 首有效声音以测试专用播放器中首个绝对幅度 >= 0.002 的样本回调为近似，不是汉字强制对齐或实体扬声器声学测量。
- 用户输入与输出等待使用同一页面 performance.now()；工具往返单独使用服务端单调时钟，未混减不同进程时钟。
- {failure_note}
- 总计 {provider['responses']} 次模型响应，包含开场、工具决策、正式回答及实际触发的提醒。没有为凑齐样本重置额度。
- 有效声音样本数为 {valid_count}，不计算生产 P95 或作性能承诺。正式回答是否正确按测试资料快照核对，不代表实时官网报价。
- 本批生产会话报告的 latency.count 为 0：起始轮次绑定工具决策响应，最终语音是另一个响应，当前应用指标尚未贯穿二者。此处独立按浏览器输入和输出时间线计算全程，不能用 report 中单次 model_first_audio_ms 代替。
- 未修改生产会话、播放器、提示词或检索实现。测试脚本只增加观测与隔离数据准备。

原始证据：`output/voice-acceptance/{run_name}/browser-results.json`、`provider-results.json`、`summary.json`。

复现：先运行 `scripts/voice_acceptance/tools_fixture.ps1`，设置新的 `ACCEPTANCE_RUN=tool-latency-日期-批次`，
将 `DATABASE_PATH` 指向同名隔离库，启动 `scripts.voice_acceptance.tools_server:app`（8001 端口）；
从 frontend 目录运行 `node tests/voice-acceptance/tools.mjs`，最后从根目录运行
`python -m scripts.voice_acceptance.analyze_tools`。需要原库中的 Tally 产品及既有索引；整个新批次限十响应。
"""
    path = Path("docs/工具问答首音延迟实测.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
