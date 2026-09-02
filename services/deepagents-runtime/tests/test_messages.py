from deepagents_runtime.messages import last_user_text, resume_message, to_langchain_messages


def test_last_user_text_reads_latest_user_message():
    assert last_user_text([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": [{"type": "text", "text": "第二问"}]},
    ]) == "第二问"


def test_resume_message_accepts_string_and_answer_object():
    assert resume_message("继续") == "继续"
    assert resume_message({"answer": "停止"}) == "停止"
    assert resume_message(False) == ""


def test_to_langchain_messages_keeps_roles():
    converted = to_langchain_messages([
        {"id": "s1", "role": "system", "content": "规则"},
        {"id": "u1", "role": "user", "content": "你好"},
        {"id": "a1", "role": "assistant", "content": "在"},
    ])
    assert [message.type for message in converted] == ["system", "human", "ai"]
    assert converted[1].content == "你好"
