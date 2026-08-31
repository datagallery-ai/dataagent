export type CollaborationToolName = "ask_user" | "submit_plan";

type ChoiceOption = { label: string; value: string; description?: string };

export type CollaborationResponseLayout = {
  recapSide: "assistant";
  choiceSide: "inline";
  planRenderer: "markdown" | undefined;
};

export function collaborationResponseLayout(
  toolName: CollaborationToolName,
): CollaborationResponseLayout {
  return {
    recapSide: "assistant",
    choiceSide: "inline",
    planRenderer: toolName === "submit_plan" ? "markdown" : undefined,
  };
}

export function formatCollaborationResponseDisplay(
  toolName: CollaborationToolName,
  response: unknown,
  options: ChoiceOption[] = [],
): string {
  if (toolName === "submit_plan") {
    if (response && typeof response === "object") {
      const record = response as { action?: string; feedback?: string };
      if (record.action === "approved") return "Plan approved";
      if (record.action === "rejected") {
        return record.feedback?.trim()
          ? `Plan rejected: ${record.feedback.trim()}`
          : "Plan rejected";
      }
    }
    return "Plan submitted for review";
  }

  if (typeof response === "string") {
    const trimmed = response.trim();
    const matched = options.find((option) => option.value === trimmed);
    return matched?.label ?? trimmed;
  }

  if (Array.isArray(response)) {
    const labels = response
      .map((item) => {
        const value = typeof item === "string" ? item.trim() : String(item);
        const matched = options.find((option) => option.value === value);
        return matched?.label ?? value;
      })
      .filter(Boolean);
    return labels.length > 0 ? labels.join(", ") : "Response submitted";
  }

  if (typeof response === "number" || typeof response === "boolean") {
    return String(response);
  }

  if (response && typeof response === "object") {
    try {
      return JSON.stringify(response);
    } catch {
      return "Response submitted";
    }
  }

  return "Response submitted";
}
