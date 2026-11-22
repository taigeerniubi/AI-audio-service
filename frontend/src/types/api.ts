// 定义浏览器与后端交换的产品、检索和会话数据类型。
export type Product = {
  id: string;
  version: number;
  name: string;
  audience: string;
  features: string;
  pricing: string;
  limitations: string;
  opening: string;
  goal: string;
  style: string;
};
export type Message = {
  id: string;
  role: string;
  text: string;
  final: boolean;
  interrupted?: boolean;
  response_id?: string;
  epoch?: number;
  played_ms?: number;
};
export type Doc = {
  id: string;
  name: string;
  characters: number;
  index_status?: string;
  chunks?: number;
  warnings?: string[];
};
export type DocumentProcessing = {
  id: string;
  name: string;
  stage:
    | "uploading"
    | "validating"
    | "parsing"
    | "embedding"
    | "saving"
    | "completed"
    | "failed";
  started_at: number;
  finished_at?: number;
  chunks?: number;
  document?: Doc;
  error?: string;
};
export type Source = {
  source: string;
  text: string;
  pages?: number[];
  headings?: string[];
  source_urls?: string[];
  score?: number;
  bm25_rank?: number | null;
  vector_rank?: number | null;
  retrieval_mode?: string;
};
export type Health = {
  configured: boolean;
  model: string;
  missing: string[];
  region: string;
  voice?: string;
  voices?: { id: string; name: string }[];
  turn_detection?: TurnDetection;
  vad_silence_ms?: number;
};
export type TurnDetection = "server_vad" | "smart_turn_v2";
export type Metric = {
  kind: string;
  [key: string]: number | string | null | undefined;
};
export type Session = {
  id: string;
  created_at: string;
  status: string;
  product_id: string;
};
export type Report = {
  session_id: string;
  model?: string;
  duration_seconds?: number;
  reason?: string;
  estimated_cost_cny?: number | null;
  missing_usage_responses?: number;
  cost_note?: string;
  transcripts: Message[];
  metrics: Metric[];
  measurement_note?: string;
  latency?: { count: number; p50: number | null; p95: number | null };
  actions: {
    name: string;
    arguments: Record<string, unknown>;
    result: Record<string, unknown>;
  }[];
};
