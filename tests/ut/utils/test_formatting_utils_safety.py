from dataagent.utils.formatting_utils import mask_sensitive_connection_info, wrap_print


def test_wrap_print_handles_tree_prefix_wider_than_width(capsys):
    line = "│   │   ├─ very long tree item"

    wrap_print(line, width=4)

    assert "very long tree item" in capsys.readouterr().out


def test_mask_sensitive_connection_info_keeps_url_endpoint_visible():
    text = "db=mysql+pymysql://user:plain-password@db.internal:3306/app"

    masked = mask_sensitive_connection_info(text)

    assert "plain-password" not in masked
    assert masked == "db=mysql+pymysql://***:***@db.internal:3306/app"
