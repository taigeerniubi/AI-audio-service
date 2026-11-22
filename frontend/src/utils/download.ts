// 把会话报告导出为本地 JSON 文件。
/**
 * 下载对象的 JSON 表示，并延迟释放临时对象地址。
 *
 * @param name - 浏览器保存的文件名。
 * @param value - 待序列化的报告或业务数据。
 */
export function download(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
