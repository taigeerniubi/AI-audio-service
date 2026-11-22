// 验证助手字幕由音频播放驱动、暂停不前进，打断后丢弃未显示文本与迟到事件。
import test from "node:test";
import assert from "node:assert/strict";
import { PlaybackCaptions } from "../src/features/studio/audio/PlaybackCaptions.ts";

const message = {
  id: "a",
  response_id: "r",
  epoch: 0,
  role: "assistant",
  text: "一二三四五六七八九十",
  final: true,
};

test("text arriving early stays hidden and generation completion does not finish subtitles", () => {
  const visible = [];
  const captions = new PlaybackCaptions((row) => visible.push(row));
  captions.text(message);
  captions.audio("a", "r", 0, 240000);
  captions.generated("r");
  assert.equal(visible.length, 0);
  captions.progress("a", "r", 0, 120000);
  assert.equal(visible.at(-1).text, "一二三四");
  assert.equal(visible.at(-1).final, false);
  const count = visible.length;
  captions.progress("a", "r", 0, 120000);
  assert.equal(visible.length, count);
  captions.complete("r", 0);
  assert.equal(visible.at(-1).text, message.text);
  assert.equal(visible.at(-1).final, true);
});

test("interrupt freezes the displayed prefix and rejects late text audio progress and completion", () => {
  const visible = [];
  const captions = new PlaybackCaptions((row) => visible.push(row));
  captions.text(message);
  captions.audio("a", "r", 0, 240000);
  captions.generated("r");
  captions.progress("a", "r", 0, 120000);
  const receipts = captions.interrupt(1);
  assert.equal(visible.at(-1).text, "一二三四");
  assert.equal(visible.at(-1).interrupted, true);
  assert.deepEqual(receipts, [
    {
      item_id: "a",
      response_id: "r",
      epoch: 0,
      heard_characters: 4,
      played_ms: 5000,
    },
  ]);
  assert.deepEqual(captions.interrupt(1), receipts);
  captions.acknowledge(receipts);
  const count = visible.length;
  captions.text({ ...message, text: message.text + "不能出现的后续内容" });
  captions.audio("a", "r", 0, 240000);
  captions.progress("a", "r", 0, 240000);
  captions.complete("r", 0);
  assert.equal(visible.length, count);
  captions.text({ ...message, id: "b", response_id: "r2", epoch: 1 });
  captions.audio("b", "r2", 1, 24000);
  captions.generated("r2");
  captions.complete("r2", 1);
  assert.equal(visible.at(-1).id, "b");
  assert.equal(visible.at(-1).text, message.text);
});

test("unplayed text has an empty interruption receipt and Unicode counts remain consistent", () => {
  const visible = [];
  const captions = new PlaybackCaptions((row) => visible.push(row));
  captions.text({ ...message, text: "甲😀乙丙" });
  assert.equal(captions.interrupt(1)[0].heard_characters, 0);
  assert.equal(visible.at(-1).text, "");
  captions.text({
    ...message,
    id: "b",
    response_id: "r2",
    epoch: 1,
    text: "甲😀乙丙",
  });
  captions.audio("b", "r2", 1, 96000);
  captions.generated("r2");
  captions.progress("b", "r2", 1, 60000);
  assert.equal(visible.at(-1).text, "甲😀");
  const receipts = captions.interrupt(2);
  assert.equal(receipts.find((row) => row.item_id === "b").heard_characters, 2);
  assert.equal(
    captions.interrupt(1).some((row) => row.item_id === "b"),
    false,
  );
});
