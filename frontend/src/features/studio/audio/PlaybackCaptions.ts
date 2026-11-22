// 用真实音频消费进度逐字揭示字幕，并冻结被打断回复的已显示前缀。
import type { Message } from "../../../types/api";

export type PlaybackReceipt = {
  item_id: string;
  response_id: string;
  epoch: number;
  heard_characters: number;
  played_ms: number;
};
type Row = {
  message: Message;
  epoch: number;
  received: number;
  played: number;
  complete: boolean;
  generated: boolean;
  interrupted: boolean;
  visible: number;
  signature: string;
};

/** 为没有字级时间戳的实时音频估算发音权重；标点分配停顿而非单独抢先出现。 */
function weight(character: string) {
  if (/\s/u.test(character)) return 0.12;
  if (/[，。！？；：,.!?;:]/u.test(character)) return 0.65;
  if (/[A-Za-z]/u.test(character)) return 0.65;
  return 1;
}

export class PlaybackCaptions {
  private rows = new Map<string, Row>();
  private completed = new Set<string>();
  private cancelled = new Set<string>();
  private onChange: (message: Message) => void;
  private epoch = 0;

  /** 绑定字幕更新回调；用户转录继续由会话层立即展示。 */
  constructor(onChange: (message: Message) => void) {
    this.onChange = onChange;
  }

  /** 创建可先收到音频或先收到文字的输出项，拒绝过期播放版本。 */
  private row(itemId: string, responseId: string, epoch: number) {
    if (epoch !== this.epoch || this.cancelled.has(responseId)) return;
    let row = this.rows.get(itemId);
    if (!row) {
      row = {
        message: {
          id: itemId,
          response_id: responseId,
          role: "assistant",
          text: "",
          final: false,
        },
        epoch,
        received: 0,
        played: 0,
        complete: this.completed.has(responseId),
        generated: false,
        interrupted: false,
        visible: 0,
        signature: "",
      };
      this.rows.set(itemId, row);
    }
    return row;
  }

  /** 保存模型生成的全文，不因文字提前到达而提前展示。 */
  text(message: Message) {
    const responseId = message.response_id;
    if (!responseId) return;
    const row = this.row(message.id, responseId, message.epoch ?? this.epoch);
    if (!row || row.interrupted || message.interrupted) return;
    row.message = message;
    this.publish(row);
  }

  /** 记录实际收到的 PCM 长度，用于生成结束后校准整段字幕的播放进度。 */
  audio(itemId: string, responseId: string, epoch: number, samples: number) {
    const row = this.row(itemId, responseId, epoch);
    if (row) row.received += samples;
  }

  /** 只根据 Worklet 确认的已渲染样本数推进字幕，不启动独立打字计时器。 */
  progress(itemId: string, responseId: string, epoch: number, samples: number) {
    const row = this.row(itemId, responseId, epoch);
    if (!row || row.interrupted) return;
    row.played = Math.max(row.played, Math.min(row.received, samples));
    this.publish(row);
  }

  /** 模型已生成完毕只固定音频长度，字幕仍需等待实际播放。 */
  generated(responseId: string) {
    for (const row of this.rows.values())
      if (row.message.response_id === responseId) {
        row.generated = true;
        this.publish(row);
      }
  }

  /** 播放完成后显示最终全文；取消后的迟到完成不能恢复已丢弃字幕。 */
  complete(responseId: string, epoch: number) {
    if (epoch !== this.epoch || this.cancelled.has(responseId)) return;
    this.completed.add(responseId);
    for (const row of this.rows.values())
      if (row.message.response_id === responseId) {
        row.complete = true;
        row.played = row.received;
        this.publish(row);
      }
  }

  /** 立即冻结已显示前缀，并给后端返回仅含计数的播放回执。 */
  interrupt(epoch: number): PlaybackReceipt[] {
    this.epoch = Math.max(this.epoch, epoch);
    const receipts: PlaybackReceipt[] = [];
    for (const row of this.rows.values()) {
      if (row.complete || row.epoch >= epoch) continue;
      row.interrupted = true;
      this.cancelled.add(row.message.response_id!);
      this.publish(row);
      receipts.push({
        item_id: row.message.id,
        response_id: row.message.response_id!,
        epoch: row.epoch,
        heard_characters: row.visible,
        played_ms: Math.floor(row.played / 24),
      });
      row.message = {
        ...row.message,
        text: Array.from(row.message.text).slice(0, row.visible).join(""),
      };
    }
    return receipts;
  }

  /** 回执发送后释放已取消字幕，旧响应 ID 仍保留在拒收集合中。 */
  acknowledge(receipts: PlaybackReceipt[]) {
    for (const receipt of receipts)
      if (this.rows.get(receipt.item_id)?.interrupted)
        this.rows.delete(receipt.item_id);
  }

  /** 根据音频时间揭示前缀；接口无字级时间戳，因此这是播放驱动的近似对齐。 */
  private publish(row: Row) {
    const characters = Array.from(row.message.text);
    if (!row.interrupted) {
      if (row.complete) row.visible = characters.length;
      else if (row.played > 0 && characters.length) {
        const weights = characters.map(weight);
        const total = weights.reduce((sum, value) => sum + value, 0);
        // 未生成完时按保守语速估算；总音频确定后按真实长度校准。始终保留尾字到播完。
        const duration = row.generated
          ? row.received
          : Math.max(row.received, (total / 4.5) * 24000);
        const target =
          (Math.max(0, row.played - 2400) / Math.max(1, duration)) * total;
        let accumulated = 0,
          visible = 0;
        for (const value of weights) {
          accumulated += value;
          if (accumulated > target) break;
          visible++;
        }
        row.visible = Math.max(
          row.visible,
          Math.min(visible, characters.length - 1),
        );
      }
    }
    const message = {
      ...row.message,
      text: characters.slice(0, row.visible).join(""),
      final: row.complete,
      interrupted: row.interrupted,
      played_ms: Math.floor(row.played / 24),
    };
    const signature = `${message.text}|${message.final}|${message.interrupted}`;
    if (signature === row.signature || (!message.text && !row.interrupted))
      return;
    row.signature = signature;
    this.onChange(message);
  }
}
