/**
 * 404 — a dead end should still offer a way out.
 */
import { navigate } from "../app/router";
import { Button, EmptyState, Panel } from "../ui/primitives";

export function NotFound({ path }: { path: string }) {
  return (
    <>
      <div className="page-head">
        <div className="page-head__text">
          <div className="eyebrow">
            <span className="label">404</span>
          </div>
          <h1 className="page-title">这条路不存在</h1>
          <p className="page-sub">
            <span className="mono">{path}</span> 没有对应的页面。
          </p>
        </div>
      </div>
      <Panel>
        <EmptyState
          title="找不到页面"
          detail="控制台的路由只有事故、证据、拓扑、评测和运维这几条。链接可能来自旧版本，或者手输错了。"
          action={
            <div className="row" style={{ justifyContent: "center" }}>
              <Button variant="primary" onClick={() => navigate("/")}>
                回到概览
              </Button>
              <Button onClick={() => navigate("/incidents")}>看事故</Button>
            </div>
          }
        />
      </Panel>
    </>
  );
}
