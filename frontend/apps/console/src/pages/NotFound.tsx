/**
 * 404 — a dead end should still offer a way out.
 */
import { Link } from "../app/link";
import { EmptyState, Ident, Panel } from "../ui/primitives";

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
            <Ident className="mono">{path}</Ident> 没有对应的页面。
          </p>
        </div>
      </div>
      <Panel>
        <EmptyState
          title="找不到页面"
          detail="控制台的路由只有事故、证据、拓扑、评测和运维这几条。链接可能来自旧版本，或者手输错了。"
          action={
            <div className="row" style={{ justifyContent: "center" }}>
              {/* Links, not buttons: the browser then gives the operator
                  middle-click, Cmd/Ctrl-click and "copy link address". */}
              <Link to="/" className="btn btn--primary btn--md">
                回到概览
              </Link>
              <Link to="/incidents" className="btn btn--default btn--md">
                看事故
              </Link>
            </div>
          }
        />
      </Panel>
    </>
  );
}
