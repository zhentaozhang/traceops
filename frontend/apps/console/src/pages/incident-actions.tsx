/**
 * The four incident actions.
 *
 * These buttons used to be rendered with no handler at all. The control plane
 * has had the endpoints for a while (`POST /incidents/{id}/assign|ack|confirm|transitions`),
 * so the console was showing an operator a set of controls that did nothing —
 * the most expensive kind of lie in an incident tool, because the person
 * pressing them believes they handed the incident to somebody.
 *
 * Each action opens a dialog, because each one is a decision with
 * consequences:
 *   指派   — names a human; the record of who owns the incident
 *   认领   — "I'm on it"; sets the first acknowledgement time
 *   确认   — accepts or rejects a conclusion; irreversible by design
 *   流转   — moves the incident along the status machine
 *
 * The buttons are enabled only when the backend would accept the action
 * (see `domain/incident-status.ts`), so a disabled control explains itself
 * instead of failing after the click.
 */
import { useId, useState } from "react";
import { dataSource } from "../data/source";
import {
  allowedTransitions,
  canAcknowledge,
  canConfirm,
  type IncidentStatus,
  parseIncidentStatus,
  TRANSITION_LABELS,
} from "../domain/incident-status";
import type { IncidentVM } from "../domain/types";
import { Field, Modal, useAction } from "../ui/overlay";
import { Button, Ident } from "../ui/primitives";

type Dialog = "assign" | "confirm" | "transition" | null;

export function IncidentActions({
  incident,
  onUpdated,
}: {
  incident: IncidentVM;
  onUpdated: (next: IncidentVM) => void;
}) {
  const [dialog, setDialog] = useState<Dialog>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const ack = useAction();
  const transitions = allowedTransitions(incident.status);
  const canAck = canAcknowledge(incident.status, incident.acknowledgedAt);
  const canConfirmNow = canConfirm(incident.status, incident.confirmation);
  const terminal = transitions.length === 0;

  return (
    <div className="wb-actions">
      <Button size="sm" onClick={() => setDialog("assign")}>
        {incident.assignee ? "改派" : "指派"}
      </Button>

      <Button
        size="sm"
        onClick={() =>
          void ack.run(async () => {
            const next = await dataSource.acknowledgeIncident(incident.id);
            onUpdated(next);
            setNotice({ tone: "ok", text: "已认领。" });
          })
        }
        disabled={!canAck || ack.pending}
        title={
          canAck
            ? "标记为「我正在处理」"
            : incident.acknowledgedAt
              ? "已经认领过了，首次认领时间不会被覆盖"
              : "已解决的事故没有可认领的东西"
        }
      >
        {ack.pending ? "认领中…" : "认领"}
      </Button>

      <Button
        size="sm"
        onClick={() => setDialog("confirm")}
        disabled={!canConfirmNow}
        title={
          canConfirmNow
            ? "接受或拒绝这一轮的结论"
            : incident.confirmation
              ? "结论已经确认过，不能改写"
              : "只有 DIAGNOSED 的事故才有结论可确认"
        }
      >
        确认结论
      </Button>

      <Button
        size="sm"
        variant="primary"
        onClick={() => setDialog("transition")}
        disabled={terminal}
        title={terminal ? "终态没有出边" : `可流转到 ${transitions.join(" / ")}`}
      >
        流转
      </Button>

      {ack.error && (
        <p className="wb-actions__error" role="alert">
          {ack.error}
        </p>
      )}
      {notice && (
        <p className="wb-actions__ok" role="status">
          {notice.text}
        </p>
      )}

      {dialog === "assign" && (
        <AssignDialog
          incident={incident}
          onClose={() => setDialog(null)}
          onDone={(next) => {
            onUpdated(next);
            setNotice({ tone: "ok", text: `已指派给 ${next.assignee ?? ""}。` });
            setDialog(null);
          }}
        />
      )}

      {dialog === "confirm" && (
        <ConfirmDialog
          incident={incident}
          onClose={() => setDialog(null)}
          onDone={(next, text) => {
            onUpdated(next);
            setNotice({ tone: "ok", text });
            setDialog(null);
          }}
        />
      )}

      {dialog === "transition" && (
        <TransitionDialog
          incident={incident}
          targets={transitions}
          onClose={() => setDialog(null)}
          onDone={(next, text) => {
            onUpdated(next);
            setNotice({ tone: "ok", text });
            setDialog(null);
          }}
        />
      )}
    </div>
  );
}

/* ── assign ─────────────────────────────────────────────────────────────── */

function AssignDialog({
  incident,
  onClose,
  onDone,
}: {
  incident: IncidentVM;
  onClose: () => void;
  onDone: (next: IncidentVM) => void;
}) {
  const fieldId = useId();
  const [name, setName] = useState(incident.assignee ?? "");
  const action = useAction();

  return (
    <Modal
      title={incident.assignee ? "改派负责人" : "指派负责人"}
      description="负责人留空的话，等于没人负责——这比空着更危险，所以名字不能为空。"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            disabled={action.pending || name.trim().length === 0}
            onClick={() =>
              void action.run(async () => {
                const next = await dataSource.assignIncident(incident.id, name);
                onDone(next);
              })
            }
          >
            {action.pending ? "提交中…" : "指派"}
          </Button>
        </>
      }
    >
      <Field id={fieldId} label="负责人" hint="例：lin.zhao。允许改派——人会休假、会换班。">
        <input
          id={fieldId}
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="例：lin.zhao"
        />
      </Field>
      {action.error && (
        <p className="field__error" role="alert">
          {action.error}
        </p>
      )}
    </Modal>
  );
}

/* ── confirm ─────────────────────────────────────────────────────────────── */

function ConfirmDialog({
  incident,
  onClose,
  onDone,
}: {
  incident: IncidentVM;
  onClose: () => void;
  onDone: (next: IncidentVM, text: string) => void;
}) {
  const fieldId = useId();
  const [by, setBy] = useState(incident.assignee ?? "");
  const action = useAction();

  const submit = (accepted: boolean) => {
    void action.run(async () => {
      const next = await dataSource.confirmIncident(incident.id, by, accepted);
      onDone(next, accepted ? "已接受这一轮的结论。" : "已拒绝这一轮的结论。");
    });
  };

  return (
    <Modal
      title="确认这一轮的结论"
      description="确认是一次决定，不能改写——确认人和当时的结论会被记下来。拒绝意味着这个根因不成立，需要重新调查。"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="danger"
            disabled={action.pending || by.trim().length === 0}
            onClick={() => submit(false)}
          >
            拒绝结论
          </Button>
          <Button
            variant="primary"
            disabled={action.pending || by.trim().length === 0}
            onClick={() => submit(true)}
          >
            {action.pending ? "提交中…" : "接受结论"}
          </Button>
        </>
      }
    >
      <Field id={fieldId} label="确认人" hint="留空不行——结论要能追责到人。">
        <input
          id={fieldId}
          className="input"
          value={by}
          onChange={(e) => setBy(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="例：lin.zhao"
        />
      </Field>
      {action.error && (
        <p className="field__error" role="alert">
          {action.error}
        </p>
      )}
    </Modal>
  );
}

/* ── transition ──────────────────────────────────────────────────────────── */

function TransitionDialog({
  incident,
  targets,
  onClose,
  onDone,
}: {
  incident: IncidentVM;
  targets: IncidentStatus[];
  onClose: () => void;
  onDone: (next: IncidentVM, text: string) => void;
}) {
  const groupId = useId();
  const [target, setTarget] = useState<IncidentStatus | null>(targets[0] ?? null);
  const action = useAction();
  const current = parseIncidentStatus(incident.status);

  return (
    <Modal
      title="流转事故状态"
      description="状态机是白名单：只有列出来的目标才是合法流转。"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            disabled={action.pending || target === null}
            onClick={() =>
              void action.run(async () => {
                if (!target) return;
                const next = await dataSource.transitionIncident(incident.id, target);
                onDone(next, `已流转到 ${TRANSITION_LABELS[target]}。`);
              })
            }
          >
            {action.pending ? "提交中…" : "确认流转"}
          </Button>
        </>
      }
    >
      {/* A radio group of native radio inputs. No role="radiogroup" needed:
          a fieldset with a legend already groups them, and the native inputs
          give the arrow-key behaviour for free. */}
      <fieldset className="choices" aria-labelledby={groupId}>
        <legend className="field__label" id={groupId}>
          目标状态
        </legend>
        {targets.map((t) => (
          <label key={t} className="choices__item">
            <input
              type="radio"
              name={`transition-${groupId}`}
              value={t}
              checked={target === t}
              onChange={() => setTarget(t)}
            />
            <span>
              <span className="choices__label">{TRANSITION_LABELS[t]}</span>
              <span className="mono faint">{t}</span>
            </span>
          </label>
        ))}
        {current && (
          <p className="field__hint">
            当前：<Ident className="mono">{current}</Ident>
          </p>
        )}
      </fieldset>
      {action.error && (
        <p className="field__error" role="alert">
          {action.error}
        </p>
      )}
    </Modal>
  );
}
