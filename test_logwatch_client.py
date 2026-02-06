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
