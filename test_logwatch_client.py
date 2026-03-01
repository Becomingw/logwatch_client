"""LogWatch 客户端基础测试"""

import logwatch_client


def test_load_config_empty():
    """测试：配置文件不存在时返回空字典"""
    import tempfile
    from pathlib import Path
    
    original_path = logwatch_client.CONFIG_PATH
    try:
        logwatch_client.CONFIG_PATH = Path(tempfile.gettempdir()) / "non_existent_lwconfig"
        config = logwatch_client.load_config()
        assert config == {}
    finally:
        logwatch_client.CONFIG_PATH = original_path


def test_get_int_config():
    """测试：从配置中读取整数值"""
    config = {"upload_interval_seconds": "5", "invalid": "abc"}
    
    assert logwatch_client._get_int_config(config, "upload_interval_seconds", 2) == 5
    
    assert logwatch_client._get_int_config(config, "invalid", 10) == 10
    
    assert logwatch_client._get_int_config(config, "nonexistent", 42) == 42


def test_precheck_command_exists():
    """测试：预检查存在的命令"""
    result = logwatch_client.precheck_command(["python", "--version"])
    assert result == 0


def test_precheck_command_not_exists():
    """测试：预检查不存在的命令"""
    result = logwatch_client.precheck_command(["nonexistent_command_12345"])
    assert result == 127


def test_build_task_email_is_mobile_friendly_table_layout():
    """测试：HTML 模板使用邮件客户端友好的表格布局，不依赖 flex"""
    _, plain, html = logwatch_client.build_task_email(
        task_name="demo-task",
        machine="demo-machine",
        command="python long_running_test.py",
        status="failed",
        exit_code=1,
        elapsed_seconds=73,
    )

    assert "table role=\"presentation\"" in html
    assert "display: flex" not in html
    assert "&#9679;" in html
    assert "[ERR]" in plain


def test_build_task_email_escapes_command_html():
    """测试：命令内容会做 HTML 转义，避免邮件内容被破坏"""
    _, _, html = logwatch_client.build_task_email(
        task_name="demo-task",
        machine="demo-machine",
        command="<script>alert('x')</script>",
        status="success",
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in html


def test_transport_state_machine_transitions(tmp_path):
    """测试：上传状态机从 online -> retrying -> offline_giveup -> task_deleted"""
    original_queue_db_path = logwatch_client.QUEUE_DB_PATH
    try:
        logwatch_client.QUEUE_DB_PATH = tmp_path / "queue.db"
        uploader = logwatch_client.LogUploader(
            server="http://127.0.0.1:8000",
            task_id="task-state-test",
            log_file=tmp_path / "state.log",
            user_id="u1",
            user_token="ut_test",
            config={"upload_circuit_break_max": "2"},
        )

        assert uploader.get_transport_state() == "online"

        uploader.mark_retryable_failure("测试失败1")
        assert uploader.get_transport_state() == "retrying"

        uploader.mark_transport_success("测试恢复")
        assert uploader.get_transport_state() == "online"

        uploader.mark_retryable_failure("测试失败2")
        uploader.mark_retryable_failure("测试失败3")
        assert uploader.get_transport_state() == "offline_giveup"
        assert uploader.is_offline() is True

        uploader.mark_task_deleted("服务端删除")
        assert uploader.get_transport_state() == "task_deleted"
        assert uploader.is_task_deleted() is True
    finally:
        logwatch_client.QUEUE_DB_PATH = original_queue_db_path
