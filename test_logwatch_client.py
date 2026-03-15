"""LogWatch 客户端基础测试"""

import uuid
from unittest.mock import Mock

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


def test_generate_task_id_returns_unique_uuid4_values():
    first = logwatch_client.generate_task_id()
    second = logwatch_client.generate_task_id()

    assert first != second
    assert uuid.UUID(first).version == 4
    assert uuid.UUID(second).version == 4


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


def test_classify_conflict_response_by_detail_code():
    response = Mock()
    response.json.return_value = {
        "detail": {
            "code": "task_deleted",
            "message": "任务已被删除，拒绝后续推送",
        }
    }
    response.text = ""

    assert logwatch_client._classify_conflict_response(response) == logwatch_client.POST_TASK_DELETED


def test_classify_conflict_response_by_task_id_conflict_message():
    response = Mock()
    response.json.return_value = {
        "detail": {
            "code": "task_id_conflict",
            "message": "task_id already exists; task_id must be globally unique and never reused",
        }
    }
    response.text = ""

    assert logwatch_client._classify_conflict_response(response) == logwatch_client.POST_TASK_ID_CONFLICT


def test_classify_conflict_response_by_task_not_running_message():
    response = Mock()
    response.json.return_value = {
        "detail": {
            "code": "task_not_running",
            "message": "task not running",
        }
    }
    response.text = ""

    assert logwatch_client._classify_conflict_response(response) == logwatch_client.POST_TASK_NOT_RUNNING


def test_collect_new_logs_archives_rows_after_task_deleted(tmp_path):
    original_queue_db_path = logwatch_client.QUEUE_DB_PATH
    try:
        queue_db = tmp_path / "queue.db"
        logwatch_client.QUEUE_DB_PATH = queue_db
        log_file = tmp_path / "task.log"
        log_file.write_text("hello after delete\n")

        uploader = logwatch_client.LogUploader(
            server="http://127.0.0.1:8000",
            task_id="task-deleted-test",
            log_file=log_file,
            user_id="u1",
            user_token="ut_test",
            config={},
        )
        uploader.mark_task_deleted("测试")
        uploader._collect_new_logs()

        queue = logwatch_client.LogQueueStore(queue_db)
        conn = queue._connect()
        row = conn.execute(
            "SELECT status, content FROM log_queue WHERE task_id=? ORDER BY client_seq LIMIT 1",
            ("task-deleted-test",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["status"] == "archived"
        assert "hello after delete" in row["content"]
    finally:
        logwatch_client.QUEUE_DB_PATH = original_queue_db_path


def test_send_event_status_returns_retryable_fail_after_retries(monkeypatch):
    calls = []

    def fake_post_json_status(*args, **kwargs):
        calls.append((args, kwargs))
        return logwatch_client.POST_RETRYABLE_FAIL

    monkeypatch.setattr(logwatch_client, "post_json_status", fake_post_json_status)

    status = logwatch_client.send_event_status(
        server="http://127.0.0.1:8000",
        task_id="task-1",
        user_id="u1",
        user_token="ut_test",
        event_type="failed",
        name="demo-task",
        machine="demo-machine",
        command="python demo.py",
        retries=3,
    )

    assert status == logwatch_client.POST_RETRYABLE_FAIL
    assert len(calls) == 3


def test_should_send_completion_email_uses_retryable_fail_fallback():
    assert logwatch_client.should_send_completion_email(True, logwatch_client.POST_OK) is True
    assert logwatch_client.should_send_completion_email(False, logwatch_client.POST_RETRYABLE_FAIL) is True
    assert logwatch_client.should_send_completion_email(False, logwatch_client.POST_TASK_DELETED) is False
