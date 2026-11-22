"""汇总首次检索与两次热请求，区分本地初始化、网络阶段和轮次等待。"""

import json
import os
from pathlib import Path

from scripts.voice_acceptance.analyze_tools import analyze_session


def stage(events: list[dict], phase: str, query_index: int | None = None) -> float:
    """汇总同一阶段的观测，不跨阶段重复累加嵌套耗时。

    Args:
        events: 已筛选到当前查询或会话的探针事件。
        phase: 需要读取的单一阶段名称。
        query_index: 可选的查询编号。

    Returns:
        毫秒数；阶段不存在时返回零。
    """
    return round(sum(
        row["elapsed_ms"] for row in events
        if row["phase"] == phase and (query_index is None or row.get("query_index") == query_index)
    ), 3)


def profile_session(session: dict, trial: int, provider: list[dict], probe: dict) -> dict:
    """将原浏览器首音指标与同次查询的后端阶段关联。

    Args:
        session: 一次独立语音会话的浏览器记录。
        trial: 从一开始的请求序号。
        provider: 原始脱敏模型事件。
        probe: 同一服务进程产生的计时和音频帧元数据。

    Returns:
        包含轮次等待、冷缓存证据及 HTTP 阶段的完整样本。

    Raises:
        ValueError: 探针与会话无法一对一关联。
    """
    row = analyze_session(session, trial, provider)
    events = session["snapshot"]["events"]
    user_end = session["snapshot"]["inputs"][0]["captureEndAt"]
    stopped = next(
        event for event in events if event["at"] >= user_end and event["kind"] == "server"
        and event["event"]["type"] == "trace"
        and event["event"]["event"]["type"] == "input_audio_buffer.speech_stopped"
    )
    connection = next(event["connection"] for event in provider if event.get("response_id") == row["response_id"])
    cloud_stop = next(
        event for event in provider if event["connection"] == connection
        and event["type"] == "input_audio_buffer.speech_stopped" and event.get("reason") != "turn_invalid"
    )
    frame_list = [event for event in probe["audio_frames"] if event["connection"] == connection]
    active = [event for event in frame_list if event["active"]]
    voice_frames = [event for event in frame_list if active[0]["sequence"] <= event["sequence"] <= active[-1]["sequence"]]
    query_events = [event for event in probe["events"] if event.get("query_index") == trial]
    loads = [event for event in query_events if event["phase"] == "tokenizer_load"]
    searches = [event for event in query_events if event["phase"] == "knowledge_search"]
    if len(loads) != 1 or len(searches) != 1:
        raise ValueError("每个样本应匹配一次分词器访问和一次检索")
    load = loads[0]
    end_wait = round(stopped["at"] - user_end, 1)
    row.update({
        "condition": "local_first_query" if trial == 1 else "local_warm_query",
        "connection": connection,
        "user_end_to_stop_event_ms": end_wait,
        "stop_event_to_tool_result_ms": round(row["user_end_to_tool_result_ms"] - end_wait, 1),
        "backend_last_audio_send_to_stop_event_ms": round((cloud_stop["monotonic"] - active[-1]["ended"]) * 1000, 1),
        "audio_send_max_ms": max(event["send_ms"] for event in frame_list),
        "audio_send_gap_during_speech_max_ms": round(max(
            (after["started"] - before["started"]) * 1000 for before, after in zip(voice_frames, voice_frames[1:])
        ), 1),
        "backend_active_frames": len(active),
        "browser_active_frames": session["snapshot"]["inputs"][0]["nonSilentFrames"],
        "tokenizer_cache": "miss" if load["cache_after"]["misses"] > load["cache_before"]["misses"] else "hit",
        "tokenizer_ms": stage(query_events, "tokenizer_load"),
        "knowledge_init_before_question_ms": stage([
            event for event in probe["events"] if event.get("knowledge_index") == trial
        ], "knowledge_init"),
        "realtime_connect_before_question_ms": stage([
            event for event in probe["events"] if event.get("connection") == connection
        ], "realtime_connect"),
        "search_ms": stage(query_events, "knowledge_search"),
        "embedding_total_ms": stage(query_events, "embedding_total"),
        "http_client_init_ms": stage(query_events, "http_client_init"),
        "http_post_ms": stage(query_events, "http_post"),
        "tcp_connect_ms": stage(query_events, "http.connection.connect_tcp"),
        "tls_ms": stage(query_events, "http.connection.start_tls"),
        "response_headers_wait_ms": stage(query_events, "http.http11.receive_response_headers"),
        "response_body_ms": stage(query_events, "http.http11.receive_response_body"),
        "query_sha256": searches[0]["query_sha256"],
        "query_chars": searches[0]["query_chars"],
    })
    row["search_outside_embedding_ms"] = round(row["search_ms"] - row["embedding_total_ms"], 3)
    row["tcp_tls_ms"] = round(row["tcp_connect_ms"] + row["tls_ms"], 3)
    return row


def main() -> None:
    """校验一冷两热实验条件并生成可复核的 JSON 和本地报告。"""
    run_name = os.environ["ACCEPTANCE_RUN"]
    folder = Path("output/voice-acceptance") / run_name
    browser = json.loads((folder / "browser-results.json").read_text(encoding="utf-8"))
    provider = json.loads((folder / "provider-results.json").read_text(encoding="utf-8"))
    probe = json.loads((folder / "cold-warm-probe.json").read_text(encoding="utf-8"))
    if browser["experiment"]["server_pid"] != probe["metadata"]["pid"]:
        raise ValueError("实验跨越服务进程，不能作为一冷两热对照")
    if probe["metadata"]["tokenizer_cache_at_install"]["currsize"] != 0 or len(browser["sessions"]) != 3:
        raise ValueError("需要一个空分词器缓存的新进程及三个完整会话")
    rows = [profile_session(session, i, provider["events"], probe) for i, session in enumerate(browser["sessions"], 1)]
    if [row["tokenizer_cache"] for row in rows] != ["miss", "hit", "hit"]:
        raise ValueError("没有观测到首次加载及后续两次本地缓存命中")
    warm_slower = any(row["user_end_to_first_audible_ms"] > rows[0]["user_end_to_first_audible_ms"] for row in rows[1:])
    finding = (
        "本批出现本地已预热但总等待更长的样本，不能用本地冷启动解释全部差异。"
        if warm_slower else "本批后两次总等待较短，但只有三次观测，不能把全部差异归因于本地冷启动。"
    )
    summary = {
        "run": run_name, "experiment": browser["experiment"], "metadata": probe["metadata"],
        "samples": rows, "model_responses": provider["responses"], "harness_errors": browser["errors"],
        "provider_errors": [event for event in provider["events"] if event["type"] == "error"],
        "finding": finding,
    }
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    phases = [
        ("用户说完至正式回答有效声音", "user_end_to_first_audible_ms"),
        ("用户说完至收到轮次结束事件", "user_end_to_stop_event_ms"),
        ("轮次结束事件至工具结果返回", "stop_event_to_tool_result_ms"),
        ("工具结果返回至回答有效声音", "tool_result_to_audible_ms"),
        ("检索函数总计", "search_ms"),
        ("其中：本地分词器加载或缓存读取", "tokenizer_ms"),
        ("其中：Embedding HTTP 完整往返", "http_post_ms"),
        ("HTTP 子阶段：TCP 连接（含解析等）", "tcp_connect_ms"),
        ("HTTP 子阶段：TLS 握手", "tls_ms"),
        ("HTTP 子阶段：等待响应头", "response_headers_wait_ms"),
        ("检索器构建，发生在提问前", "knowledge_init_before_question_ms"),
        ("云端连接建立，发生在提问前", "realtime_connect_before_question_ms"),
        ("后端发完最后有效帧至收到轮次结束事件", "backend_last_audio_send_to_stop_event_ms"),
    ]
    table = "\n".join(f"| {name} | " + " | ".join(f"{row[key]:.3f}" for row in rows) + " |" for name, key in phases)
    report = f"""# 本地首次检索与热请求延迟对照

**{finding}** 首次本地分词器加载实测 {rows[0]['tokenizer_ms']:.1f} ms，后两次命中缓存。
TCP/TLS 建连合计分别为 {'、'.join(f"{row['tcp_tls_ms']:.1f}" for row in rows)} ms；
查询 HTTP 完整往返分别为 {'、'.join(f"{row['http_post_ms']:.1f}" for row in rows)} ms。
模型轮次结束事件到达与工具返回后的语音等待也有波动，当前观测不能进一步拆出云模型内部计算与远端排队。

批次：{run_name}；PID {probe['metadata']['pid']}；同一个服务进程连续执行三个独立会话。
保持 qwen-audio-3.1-realtime-plus、smart_turn_v2、Tally 资料快照和同一合成输入语音不变。
问题：“请查询产品资料，告诉我 Pro 套餐的月付价格是多少？”
本批实际使用 {provider['responses']} 次实时模型响应及三次查询 Embedding；未重建文档索引。

## 结果（毫秒）

| 指标 | 第一次：本地首次检索 | 第二次：本地已预热 | 第三次：本地已预热 |
|---|---:|---:|---:|
{table}

阶段存在嵌套：HTTP 子阶段属于 HTTP 往返，HTTP 往返与分词器加载属于检索；不能把整张表再次相加。
等待响应头包含远程服务处理及网络返回，不能命名为纯 Embedding 模型推理耗时。

## 实验边界

- 冷启动特指本地 Python 进程内分词器缓存为空，首次检索出现 miss；后两次明确出现 hit。不是云模型权重冷启动，也不是操作系统磁盘缓存清空。
- 每次都新建千问实时连接并播放开场，再提问；开场已经调用模型。重建连接与首次检索器构造发生在提问前，单独列出，不计入说完至首声。
- 三次使用同一 WAV，SHA256 为 {browser['experiment']['fixture_sha256']}。模型生成的检索词字符数为 {[row['query_chars'] for row in rows]}，其摘要在 summary.json 中；同一句音频不保证每次生成完全相同的工具参数。
- 保留生产代码每次创建 Embedding HTTP 客户端的方式。每次均观测到 TCP/TLS 阶段，热进程不等于热连接。
- 浏览器总等待使用同一页面 performance.now()；服务端阶段单独使用 perf_counter，不混减两个进程的绝对时钟。
- 音频只记录帧号、RMS 和发送时间，不存原始录音。WebSocket send 返回表示本地发送完成，不代表远端已收到；后端最后有效帧至事件返回的时长仍包含外部网络和云端处理。
- 三个样本均使用实际工具结果后的新响应作为正式回答首声，音频有效阈值为绝对幅度 0.002。此处是合成输入与浏览器音频线程测量，不是实体麦克风与扬声器的声学验证。
- 只有一组一冷两热，不能证明生产分布或排除云端实例调度。结合分阶段证据分析，不把“第一个请求”直接等同于“所有慢请求的根因”。

复现：设置新的 `ACCEPTANCE_RUN=tool-latency-coldwarm-日期-批次` 和独立 `DATABASE_PATH`，启动
`python -m uvicorn scripts.voice_acceptance.cold_warm_server:app --host 127.0.0.1 --port 8001`；
从 frontend 目录运行 `node tests/voice-acceptance/tools.mjs`，从根目录运行
`python -m scripts.voice_acceptance.analyze_cold_warm`。禁止在同批次重启服务或用预热请求消耗首次样本。

原始证据在 `output/voice-acceptance/{run_name}/`：browser-results.json、provider-results.json、cold-warm-probe.json、summary.json。
"""
    target = Path("docs/冷启动与热请求延迟对照.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
