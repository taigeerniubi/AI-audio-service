// 封装 JSON 请求并统一呈现后端的业务错误。
/**
 * 请求 JSON 数据，非成功状态抛出面向用户的错误。
 *
 * @param url - 同源 API 路径。
 * @param init - 可选 fetch 请求配置，包括方法、请求头及正文。
 * @returns 按调用方类型参数解析的 JSON 结果。
 */
export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "请求失败，请检查填写内容。",
    );
  return data;
}
