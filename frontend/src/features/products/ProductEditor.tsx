// 编辑产品核心事实、销售话术和版本化配置。
import { useEffect, useState } from "react";
import { Check, ChevronDown, LoaderCircle, Plus } from "lucide-react";
import { api } from "../../api/client";
import type { Product } from "../../types/api";
import { KnowledgePanel } from "./KnowledgePanel";
const blank: Omit<Product, "id" | "version"> = {
  name: "知答 AI 客服",
  audience: "有针对自己公司的产品感兴趣的人",
  features: "基于产品资料回答常见问题；人工转接；对话记录；知识来源追溯。",
  pricing:
    "电脑硬件按具体型号、数量和配置询价。当前资料未提供售价、套餐价、折扣或库存，暂无可直接引用的报价。涉及 CPU、主板、内存和 SSD 时，先检索对应资料；无价格依据则说明需人工确认。不得沿用客服订阅套餐、虚构金额或承诺优惠与现货。确认报价时应注明币种、含税及运费情况、报价有效期。",
  limitations:
    "仅为作品集虚构产品；不承诺准确率、营业收益或未经确认的集成能力。",
  opening: "您好，我是知答的 AI 产品顾问。您目前最想了解哪类产品？",
  goal: "了解需求并预约产品演示，发送产品信息链接挂断电话后",
  style: "自然、简洁，每次只问一个问题，会推销自家的产品。",
};

/** 保存产品档案，并把资料管理交给独立面板。 */
export function ProductEditor({
  products,
  selected,
  setSelected,
  onSaved,
  onError,
}: {
  products: Product[];
  selected: string;
  setSelected: (id: string) => void;
  onSaved: (id: string) => Promise<void>;
  onError: (e: string) => void;
}) {
  const [form, setForm] = useState(blank);
  const [editingId, setEditingId] = useState<string | null>(selected);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    const p = products.find((p) => p.id === selected);
    if (p) {
      const { id, version: _v, ...values } = p;
      setForm(values);
      setEditingId(id);
    }
  }, [selected, products]);
  /**
   * 提交产品的新建或更新请求，再刷新产品选择和保存状态。
   *
   * @param e - 表单提交事件，需要阻止浏览器默认跳转。
   */
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    try {
      const p = await api<Product>(
        editingId ? `/api/products/${editingId}` : "/api/products",
        {
          method: editingId ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(form),
        },
      );
      setEditingId(p.id);
      await onSaved(p.id);
      setSaved(true);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  const fields = [
    ["name", "产品名称", 100],
    ["audience", "目标客户", 2000],
    ["features", "主要功能", 6000],
    ["pricing", "价格与套餐", 3000],
    ["limitations", "限制与不能承诺的事项", 3000],
    ["opening", "开场白", 500],
    ["goal", "对话目标", 1000],
    ["style", "表达风格", 1000],
  ] as const;
  /**
   * 渲染配置字段，并在内容改变后清除过期的已保存状态。
   *
   * @param field - 配置键、显示名称与字符长度限制。
   * @returns 具有可访问标签的受控输入框。
   */
  function renderField(field: (typeof fields)[number]) {
    const [key, label, max] = field;
    return (
      <label key={key}>
        {label}
        {key === "name" ? (
          <input
            required
            aria-label={label}
            maxLength={max}
            value={form[key]}
            onChange={(event) => {
              setForm({ ...form, [key]: event.target.value });
              setSaved(false);
            }}
          />
        ) : (
          <textarea
            aria-label={label}
            rows={key === "features" ? 3 : 2}
            maxLength={max}
            value={form[key]}
            onChange={(event) => {
              setForm({ ...form, [key]: event.target.value });
              setSaved(false);
            }}
          />
        )}
      </label>
    );
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>产品与知识</h1>
          <p>配置顾问了解的产品，添加回答问题时需要的资料。</p>
        </div>
        <button
          className="secondary"
          onClick={() => {
            setEditingId(null);
            setForm({ ...blank });
            setSaved(false);
          }}
        >
          <Plus size={16} />
          新建产品
        </button>
      </div>
      <div className="editor-layout">
        <section className="card product-form">
          <div className="panel-top">
            <span>{editingId ? "产品配置" : "新建产品"}</span>
            <select
              aria-label="编辑的产品"
              value={editingId || ""}
              onChange={(e) => {
                setSelected(e.target.value);
                setEditingId(e.target.value);
              }}
            >
              <option value="" disabled>
                新产品
              </option>
              {products.map((p) => (
                <option value={p.id} key={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <form onSubmit={save}>
            <div className="field-section">
              <div className="field-section-heading">
                <h3>产品基本信息</h3>
              </div>
              <p className="product-config-tip">
                让顾问知道产品是什么，以及适合谁。
              </p>
              {fields.slice(0, 3).map(renderField)}
            </div>
            <details className="form-disclosure" key={`${editingId}-pricing`}>
              <summary>
                <span>
                  <strong>价格与回答边界</strong>
                  <small>价格、套餐与不能承诺的事项</small>
                </span>
                <ChevronDown size={17} />
              </summary>
              <div>{fields.slice(3, 5).map(renderField)}</div>
            </details>
            <details
              className="form-disclosure"
              key={`${editingId}-conversation`}
            >
              <summary>
                <span>
                  <strong>顾问表达与目标</strong>
                  <small>开场白、对话目标与表达风格</small>
                </span>
                <ChevronDown size={17} />
              </summary>
              <div>{fields.slice(5).map(renderField)}</div>
            </details>
            <div className="form-bottom">
              <span className="subtle">修改仅影响之后创建的会话。</span>
              <button className="primary" disabled={saving} type="submit">
                {saving ? (
                  <LoaderCircle size={16} className="spin" />
                ) : saved ? (
                  <Check size={16} />
                ) : null}
                {saved ? "已保存" : "保存产品"}
              </button>
            </div>
          </form>
        </section>
        <KnowledgePanel
          key={editingId || "new"}
          editingId={editingId}
          productName={
            products.find((product) => product.id === editingId)?.name ||
            "新产品"
          }
          onError={onError}
        />
      </div>
    </>
  );
}
