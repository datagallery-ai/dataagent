import { describe, expect, it } from "vitest";
import { createTranslator } from "../../../i18n/translate";
import { workspaceConfigItemStatusBadge } from "../data-task-state";

const t = createTranslator("en");

describe("workspaceConfigItemStatusBadge", () => {
  it("shows not tested for builtin server-default before probe", () => {
    expect(
      workspaceConfigItemStatusBadge(
        {
          id: "server-default",
          name: "default",
          description: "",
          enabled: true,
          builtin: true,
          status: "untested",
        },
        t,
      ),
    ).toEqual({
      label: "Not tested",
      className: "bg-slate-100 text-slate-400",
    });
  });

  it("shows connected only after a successful probe", () => {
    expect(
      workspaceConfigItemStatusBadge(
        {
          id: "qwen",
          name: "Qwen",
          description: "",
          enabled: true,
          status: "connected",
        },
        t,
      ),
    ).toEqual({
      label: "Connected",
      className: "bg-emerald-50 text-emerald-700",
    });
  });

  it("shows unavailable after a failed probe", () => {
    expect(
      workspaceConfigItemStatusBadge(
        {
          id: "qwen",
          name: "Qwen",
          description: "",
          enabled: true,
          status: "failed",
        },
        t,
      ),
    ).toEqual({
      label: "Unavailable",
      className: "bg-rose-50 text-rose-700",
    });
  });
});
