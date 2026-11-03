import type { ReactNode } from "react";
import { Card, Grid, List, Skeleton, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import "../styles/resources.css";

export function PageHeader({ title, description, actions }: { title: string; description: string; actions?: ReactNode }) {
  return <div className="resource-page-heading"><div className="resource-heading-copy"><h1>{title}</h1><p>{description}</p></div>{actions && <div className="resource-heading-actions">{actions}</div>}</div>;
}

export function ResourceCard({ icon, title, description, tags, meta, actions }: {
  icon: ReactNode; title: string; description: string; tags?: ReactNode; meta?: ReactNode; actions?: ReactNode;
}) {
  return <Card size="small" className="resource-card"><div className="resource-card-heading"><span className="resource-icon">{icon}</span><strong>{title}</strong></div>
    <p className="resource-card-description">{description || "暂无用途说明"}</p><div className="resource-tags">{tags}</div>
    {meta && <div className="resource-card-meta">{meta}</div>}{actions && <div className="resource-card-actions">{actions}</div>}
  </Card>;
}

export function ResourceCollection<T extends object>({ items, columns, renderCard, rowKey, loading, empty }: {
  items: T[]; columns: ColumnsType<T>; renderCard: (item: T) => ReactNode; rowKey: keyof T; loading: boolean; empty: ReactNode;
}) {
  const screens = Grid.useBreakpoint();
  if (loading) return <div className="resource-loading" aria-label="正在加载资源" aria-busy="true"><Skeleton active paragraph={{ rows: 5 }} /></div>;
  if (screens.md) return <Table rowKey={String(rowKey)} columns={columns} dataSource={items} pagination={items.length > 20 ? { pageSize: 20, showSizeChanger: false } : false} locale={{ emptyText: empty }} scroll={{ x: "max-content" }} />;
  return <List className="resource-mobile-list" dataSource={items} rowKey={(item) => String(item[rowKey])} locale={{ emptyText: empty }} pagination={items.length > 20 ? { pageSize: 20, showSizeChanger: false, simple: true } : false}
    renderItem={(item) => <List.Item>{renderCard(item)}</List.Item>} />;
}
