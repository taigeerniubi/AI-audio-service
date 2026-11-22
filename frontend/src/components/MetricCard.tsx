// 展示单项语音会话观测指标。
/** 渲染指标名称、数值和口径说明。 */
export function MetricCard({
  icon,
  title,
  value,
  caption,
}: {
  icon: React.ReactNode;
  title: string;
  value: string;
  caption: string;
}) {
  return (
    <div className="metric-card card">
      <div className="metric-title">
        {icon}
        {title}
      </div>
      <strong>{value}</strong>
      <small>{caption}</small>
    </div>
  );
}
