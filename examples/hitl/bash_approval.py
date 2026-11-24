"""Shell authorization example: approve one native execute call at a time."""

from dataagent import HITLHandler, InteractionRequest, ResumeDecision

_APPROVE = {"允许执行", "允许", "同意", "批准", "approve", "allow", "yes", "y"}
_REJECT = {"拒绝执行", "拒绝", "不同意", "deny", "reject", "no", "n"}


class BashApprovalHandler(HITLHandler):
    """The policy matches before_tool/execute; the default trigger always asks."""

    def interaction_builder(self, context, *, params):
        command = context.tool_call["args"]["command"]
        return InteractionRequest(
            question=(
                "是否授权执行以下 Shell 命令？本次授权仅对当前调用有效。\n\n"
                f"{command}\n\n"
                "请选择允许或拒绝，也可以输入修改要求，让模型重新规划。"
            ),
            options=["允许执行", "拒绝执行", "重新规划"],
        )

    def resume_handler(self, context, *, params):
        answer = context.answer.strip()
        if answer.casefold() in _APPROVE:
            return ResumeDecision(action="continue")
        if answer.casefold() in _REJECT:
            return ResumeDecision(
                action="reject",
                message="用户拒绝授权本次 Shell 命令。请停止原样重试，并调整后续方案。",
            )
        return ResumeDecision(
            action="replan",
            message=(
                f"本次 Shell 命令尚未获得授权。用户反馈：{answer}\n"
                "请结合反馈重新规划；新的命令仍需单独授权。"
            ),
        )
